"""A projection failure must say why, not only that it happened."""

from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory import stock_issue_service as service


class StockIssueFailureReasonTests(TestCase):
    def test_the_sanitized_cause_reaches_the_caller(self) -> None:
        """A NegativeStockError once surfaced only as "was not created"."""

        reason: list[str] = []
        order = object()
        with (
            patch.object(service, "_coerce_doc", return_value=order),
            patch.object(service, "_coerce_resolved_sales", return_value=[object()]),
            patch.object(service, "_stock_issue_projection_id", return_value="PROJ-1"),
            patch.object(service, "_get_existing_reference", return_value=None),
            patch.object(service, "_find_legacy_stock_issue", return_value=None),
            patch.object(
                service,
                "_build_grouped_issue_items",
                return_value=[{"item_code": "MILK", "qty": 1}],
            ),
            patch.object(service, "_make_savepoint", return_value="sp"),
            patch.object(service, "_rollback_savepoint") as rollback,
            patch.object(service, "_log_error") as logged,
            patch.object(
                service,
                "privileged_device_api_operation",
                side_effect=ValueError("1.0 units of MILK needed in Outlet - WH"),
            ),
        ):
            result = service.create_ingredient_stock_entry(
                order, [object()], failure_reason=reason
            )

        # Behaviour is unchanged: rolled back, returned None, logged once.
        self.assertIsNone(result)
        rollback.assert_called_once_with("sp")
        self.assertEqual(logged.call_count, 1)
        # And the caller now learns why.
        self.assertEqual(len(reason), 1)
        self.assertIn("MILK", reason[0])

    def test_omitting_the_out_list_keeps_the_old_signature_working(self) -> None:
        with (
            patch.object(service, "_coerce_doc", return_value=None),
        ):
            self.assertIsNone(service.create_ingredient_stock_entry(None, []))

    def test_sanitized_message_keeps_the_exception_class(self) -> None:
        from kopos_connector.utils.diagnostics import sanitized_error_message

        message = sanitized_error_message(
            ValueError("1.0 units of MILK needed in Outlet - WH")
        )
        self.assertIn("MILK", message)
        self.assertTrue(message.strip())

    def test_log_error_forwards_the_exception(self) -> None:
        """Without the exception the sanitized logger records only the title."""

        seen: list[tuple[str, object]] = []
        with patch.object(
            service, "log_sanitized_error", side_effect=lambda t, e=None: seen.append((t, e))
        ):
            error = ValueError("insufficient stock")
            service._log_error("Ingredient stock issue projection failed", error)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][1], error)


if __name__ == "__main__":
    import unittest

    unittest.main()
