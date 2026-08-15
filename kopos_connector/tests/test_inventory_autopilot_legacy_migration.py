from __future__ import annotations

from unittest import TestCase
from unittest.mock import Mock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot import legacy_migration


class LegacyInventoryMigrationTests(TestCase):
    def test_discovery_scopes_global_item_flags_without_querying_a_company_column(self) -> None:
        captured: dict[str, object] = {}

        def get_all(doctype: str, **kwargs: object) -> list[dict[str, object]]:
            captured["doctype"] = doctype
            captured.update(kwargs)
            return [
                {
                    "name": "MONT-BLANC",
                    "item_code": "MONT-BLANC",
                    "custom_kopos_availability_mode": "force_unavailable",
                    "custom_kopos_track_stock": 1,
                    "custom_kopos_min_qty": 2,
                }
            ]

        with patch.object(legacy_migration.frappe, "get_all", side_effect=get_all):
            values = legacy_migration.discover_legacy_values(company="JiJi Cafe")

        self.assertEqual(captured["doctype"], "Item")
        self.assertNotIn("filters", captured)
        self.assertNotIn("company", captured["fields"])
        self.assertEqual(
            values,
            [
                {
                    "item": "MONT-BLANC",
                    "company": "JiJi Cafe",
                    "availability_mode": "force_unavailable",
                    "track_stock": 1,
                    "min_qty": 2,
                }
            ],
        )

    def test_missing_legacy_columns_still_produces_a_safe_empty_value_report(self) -> None:
        class ItemMeta:
            @staticmethod
            def has_field(_fieldname: str) -> bool:
                return False

        captured: dict[str, object] = {}

        def get_all(_doctype: str, **kwargs: object) -> list[dict[str, object]]:
            captured.update(kwargs)
            return [{"name": "LATTE", "item_code": "LATTE"}]

        with (
            patch.object(legacy_migration.frappe, "get_meta", return_value=ItemMeta()),
            patch.object(legacy_migration.frappe, "get_all", side_effect=get_all),
        ):
            values = legacy_migration.discover_legacy_values(company="JiJi Cafe")

        self.assertEqual(captured["fields"], ["name", "item_code"])
        self.assertEqual(values[0]["availability_mode"], "")
        self.assertIsNone(values[0]["track_stock"])
        self.assertIsNone(values[0]["min_qty"])

    def test_execute_requires_the_exact_dry_run_digest_and_is_replay_safe(self) -> None:
        values = [
            {
                "item": "MONT-BLANC",
                "company": "JiJi Cafe",
                "availability_mode": "force_unavailable",
                "track_stock": 1,
                "min_qty": 2,
            },
            {
                "item": "LATTE",
                "company": "JiJi Cafe",
                "availability_mode": "force_available",
                "track_stock": 1,
                "min_qty": 1,
            },
        ]
        digest = legacy_migration.legacy_input_digest(values)
        create_hold = Mock(return_value="HOLD-1")
        with (
            patch.object(legacy_migration, "discover_legacy_values", return_value=values),
            patch.object(legacy_migration, "create_hold", create_hold),
            patch.object(legacy_migration, "_create_off_rule") as create_off_rule,
            patch.object(legacy_migration.frappe.db, "commit") as commit,
        ):
            first = legacy_migration.execute_legacy_migration(
                warehouse="Outlet A",
                company="JiJi Cafe",
                expected_digest=digest,
            )
            second = legacy_migration.execute_legacy_migration(
                warehouse="Outlet A",
                company="JiJi Cafe",
                expected_digest=digest,
            )

        self.assertEqual(first["input_digest"], digest)
        self.assertEqual(second["input_digest"], digest)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(second["status"], "applied")
        self.assertEqual(create_hold.call_count, 2)
        self.assertEqual(create_off_rule.call_count, 2)
        self.assertEqual(commit.call_count, 2)

        with (
            patch.object(legacy_migration, "discover_legacy_values", return_value=values),
            self.assertRaisesRegex(ValueError, "input digest does not match"),
        ):
            legacy_migration.execute_legacy_migration(
                warehouse="Outlet A",
                company="JiJi Cafe",
                expected_digest="0" * 64,
            )

    def test_unknown_values_block_execution_before_any_write(self) -> None:
        values = [{
            "item": "MYSTERY",
            "company": "JiJi Cafe",
            "availability_mode": "sometimes",
            "track_stock": None,
            "min_qty": None,
        }]
        digest = legacy_migration.legacy_input_digest(values)
        with (
            patch.object(legacy_migration, "discover_legacy_values", return_value=values),
            patch.object(legacy_migration, "create_hold") as create_hold,
            patch.object(legacy_migration, "_create_off_rule") as create_off_rule,
            patch.object(legacy_migration.frappe.db, "commit") as commit,
        ):
            report = legacy_migration.execute_legacy_migration(
                warehouse="Outlet A",
                company="JiJi Cafe",
                expected_digest=digest,
            )

        self.assertEqual(report["status"], "blocked")
        create_hold.assert_not_called()
        create_off_rule.assert_not_called()
        commit.assert_not_called()

    def test_dry_run_reports_unknown_values_as_blocked(self) -> None:
        values = [{
            "item": "MYSTERY",
            "company": "JiJi Cafe",
            "availability_mode": "sometimes",
            "track_stock": None,
            "min_qty": None,
        }]
        with patch.object(legacy_migration, "discover_legacy_values", return_value=values):
            report = legacy_migration.migrate_legacy_values(
                warehouse="Outlet A",
                company="JiJi Cafe",
                dry_run=True,
            )

        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["blocked"][0]["reason"], "unknown_availability_mode")
