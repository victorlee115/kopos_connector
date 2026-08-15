from __future__ import annotations

import inspect

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api import inventory
from kopos_connector.auth import ALLOWED_DEVICE_API_PATHS, DEVICE_API_HTTP_METHODS


pytestmark = pytest.mark.inventory_regression


class TestInventoryCountConfirmationContract(FrappeTestCase):
    def test_observation_has_durable_confirmation_identity(self) -> None:
        meta = frappe.get_meta("FB Inventory Count Observation")
        for fieldname in (
            "confirmation_command_id",
            "confirmation_device_id",
            "confirmation_manager_id",
            "confirmed_at",
        ):
            self.assertIsNotNone(meta.get_field(fieldname), fieldname)

    def test_endpoint_is_device_scoped_post_and_allowlisted(self) -> None:
        parameters = inspect.signature(inventory.confirm_count_reconciliation).parameters
        self.assertEqual(set(parameters), {"device_id", "payload"})
        path = "/api/method/kopos_connector.api.confirm_count_reconciliation"
        self.assertIn(path, ALLOWED_DEVICE_API_PATHS)
        self.assertEqual(DEVICE_API_HTTP_METHODS[path], frozenset({"POST"}))

    def test_stock_reconciliation_remains_the_standard_authority(self) -> None:
        meta = frappe.get_meta("Stock Reconciliation")
        self.assertIsNotNone(meta.get_field("docstatus"))
        self.assertIsNotNone(meta.get_field("company"))
        self.assertIsNotNone(meta.get_field("items"))
