from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot import projection_worker


class InventoryProjectionWorkerTests(TestCase):
    def setUp(self) -> None:
        self.order = SimpleNamespace(
            name="FB-ORDER-SNAPSHOT-1",
            company="JiJi",
            booth_warehouse="Outlet - J",
            sale_datetime=datetime(2026, 8, 15, 9, 0, 0),
            items=[
                SimpleNamespace(
                    backend_line_uuid="line-1",
                    commercial_modifier_snapshot_json='[{"modifier":"EXTRA-SYRUP"}]',
                    item="MONT-BLANC",
                    line_id="line-1",
                    qty="2",
                    recipe="MONT-BLANC-V1",
                    recipe_hash="a" * 64,
                    recipe_version="1",
                )
            ],
        )
        self.policy = SimpleNamespace(
            name="INV-POLICY-OUTLET-J",
            automation_state="Active",
            inventory_contract_version="inventory-v1",
            cutover_token="cutover-1",
            cutover_at=datetime(2026, 8, 15, 8, 0, 0),
            expense_account=None,
            cogs_account=None,
        )

    def test_payload_hash_is_stable_for_the_immutable_sale_snapshot(self) -> None:
        identity = {
            "order": self.order.name,
            "projector_role": "inventory_material_issue",
            "inventory_contract_version": self.policy.inventory_contract_version,
            "cutover_token": self.policy.cutover_token,
        }

        first = projection_worker._inventory_projection_payload_hash(self.order, identity)
        second = projection_worker._inventory_projection_payload_hash(self.order, identity)
        self.order.items[0].qty = "3"
        changed = projection_worker._inventory_projection_payload_hash(self.order, identity)

        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_invalid_recipe_snapshot_is_logged_then_retried_as_inventory_work(self) -> None:
        events: list[str] = []

        def create_log(**_kwargs: object) -> str:
            events.append("log")
            return "INV-PROJECTION-1"

        def prepare(_order: object) -> list[object]:
            events.append("prepare")
            raise ValueError("sale-time recipe hash is missing")

        with (
            patch.object(projection_worker.frappe, "get_doc", return_value=self.order),
            patch.object(projection_worker, "_policy_for_order", return_value=self.policy),
            patch.object(projection_worker, "create_projection_log", side_effect=create_log),
            patch.object(projection_worker, "_claim_log", return_value="worker-lease"),
            patch.object(projection_worker, "_prepare_frozen_resolved_sales", side_effect=prepare),
            patch.object(projection_worker, "_record_failure") as record_failure,
        ):
            result = projection_worker.project_inventory_order(self.order.name)

        self.assertEqual(events, ["log", "prepare"])
        self.assertEqual(result["state"], "Failed")
        self.assertEqual(result["projection_log"], "INV-PROJECTION-1")
        record_failure.assert_called_once()

    def test_pre_cutover_order_is_never_reinterpreted_as_inventory_history(self) -> None:
        self.policy.cutover_at = datetime(2026, 8, 15, 10, 0, 0)

        with (
            patch.object(projection_worker.frappe, "get_doc", return_value=self.order),
            patch.object(projection_worker, "_policy_for_order", return_value=self.policy),
            patch.object(projection_worker, "create_projection_log") as create_log,
        ):
            result = projection_worker.project_inventory_order(self.order.name)

        self.assertEqual(result, {"order": self.order.name, "state": "Not Evaluated"})
        create_log.assert_not_called()

    def test_dead_letter_containment_pauses_only_the_affected_policy(self) -> None:
        with patch.object(projection_worker.frappe.db, "set_value") as set_value:
            projection_worker._pause_policy_for_integrity_failure(self.policy)

        set_value.assert_called_once_with(
            "FB Inventory Policy",
            self.policy.name,
            "automation_state",
            "Paused",
            update_modified=False,
        )
        self.assertEqual(self.policy.automation_state, "Paused")

        with patch.object(projection_worker.frappe.db, "set_value") as set_value:
            projection_worker._pause_policy_for_integrity_failure(self.policy)
        set_value.assert_not_called()
