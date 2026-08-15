from __future__ import annotations

import inspect

import frappe
import pytest
from frappe.tests.utils import FrappeTestCase

from kopos_connector.api import inventory
from kopos_connector.kopos.services.inventory_autopilot.edge_snapshot import (
    EDGE_SCHEMA_VERSION,
    _safe_task,
)


pytestmark = pytest.mark.inventory_regression


class TestEdgeSnapshotContract(FrappeTestCase):
    def test_endpoint_has_only_bound_query_inputs(self) -> None:
        parameters = inspect.signature(inventory.get_edge_snapshot).parameters
        self.assertEqual(
            set(parameters),
            {"device_id", "known_version", "known_overlay_version", "search", "limit"},
        )

    def test_operational_sources_are_installed_without_financial_device_fields(self) -> None:
        self.assertEqual(EDGE_SCHEMA_VERSION, "inventory-edge-v1")
        for doctype, fields in {
            "Item": ("name", "item_name", "stock_uom", "is_stock_item"),
            "Bin": ("item_code", "warehouse", "actual_qty", "reserved_qty"),
            "POS Profile": ("company", "warehouse"),
        }.items():
            meta = frappe.get_meta(doctype)
            for fieldname in fields:
                self.assertIsNotNone(meta.get_field(fieldname), f"{doctype}.{fieldname}")

        # The edge response is an operational surface.  These values may be
        # read privately by ERP planning, but must never be serialized to a
        # device by the task/stock read model.
        forbidden = {
            "rate", "price", "amount", "valuation_rate", "cogs", "margin",
            "value_ceiling", "supplier_terms", "currency",
        }
        safe = _safe_task(
            {
                "kind": "receiving",
                "document": "PO-TEST",
                "rate": 1,
                "value": 2,
                "lines": [{"item_code": "ITEM-TEST", "amount": 3}],
            },
            max_lines=10,
        )
        self.assertTrue(forbidden.isdisjoint(safe))
        self.assertTrue(forbidden.isdisjoint(safe["lines"][0]))
