from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api import inventory


class InventoryCountReviewTests(TestCase):
    def test_replayed_director_review_resolves_only_matching_exception(self) -> None:
        observation = {
            "name": "OBS-DIRECTOR-1",
            "task_id": "TASK-1",
            "company": "JiJi",
            "warehouse": "Outlet - KL",
            "status": "Review",
            "reconciliation": "KOPOS-COUNT-existing",
        }
        task = SimpleNamespace(name="TASK-1", company="JiJi")

        with patch.object(inventory, "_require_company_director"), patch.object(
            inventory.frappe.db, "sql", return_value=[]
        ), patch.object(
            inventory.frappe.db, "get_value", return_value=observation
        ), patch.object(
            inventory.frappe, "get_doc", return_value=task
        ), patch.object(
            inventory, "resolve_inventory_exception"
        ) as resolve, patch.object(inventory.frappe.db, "commit"):
            result = inventory.create_count_reconciliation_after_director_review(
                payload={"observation_id": "OBS-DIRECTOR-1", "reason": "Reviewed."}
            )

        self.assertEqual(result["status"], "replayed")
        resolve.assert_called_once_with(
            reason_code="inventory_count_director_review",
            company="JiJi",
            warehouse="Outlet - KL",
            source_doctype="FB Inventory Count Observation",
            source_name="OBS-DIRECTOR-1",
        )

    def test_stale_director_review_exception_carries_task_company(self) -> None:
        observation = {
            "name": "OBS-DIRECTOR-2",
            "task_id": "TASK-2",
            "warehouse": "Outlet - KL",
            "stock_watermark": "old-watermark",
            "status": "Review",
            "reconciliation": "",
        }
        task = SimpleNamespace(name="TASK-2", company="JiJi")
        with patch.object(inventory, "_require_company_director"), patch.object(
            inventory.frappe.db, "sql", side_effect=[[], [["new-watermark"]]]
        ), patch.object(
            inventory.frappe.db, "get_value", return_value=observation
        ), patch.object(
            inventory.frappe, "get_doc", return_value=task
        ), patch.object(
            inventory.frappe.db, "exists", return_value=True
        ), patch.object(
            inventory.frappe.db, "set_value"
        ), patch.object(
            inventory.frappe.db, "commit"
        ), patch.object(
            inventory, "upsert_inventory_exception", return_value="EX-STALE"
        ) as upsert:
            result = inventory.create_count_reconciliation_after_director_review(
                payload={"observation_id": "OBS-DIRECTOR-2", "reason": "Reviewed."}
            )

        self.assertEqual(result, {
            "status": "conflict",
            "observation_id": "OBS-DIRECTOR-2",
            "exception": "EX-STALE",
        })
        self.assertEqual(upsert.call_args.kwargs["company"], "JiJi")
        self.assertEqual(upsert.call_args.kwargs["warehouse"], "Outlet - KL")


if __name__ == "__main__":
    import unittest

    unittest.main()
