from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory_autopilot import count_scheduler


class _TaskDocument:
    def __init__(self) -> None:
        self.name = "INV-COUNT-00001"
        self.lines: list[dict[str, str]] = []
        self.inserted = False

    def append(self, fieldname: str, value: dict[str, str]) -> None:
        assert fieldname == "lines"
        self.lines.append(value)

    def insert(self, **_kwargs: object) -> None:
        self.inserted = True


class InventoryCountSchedulerTests(TestCase):
    def test_daily_schedule_uses_business_calendar_day(self):
        self.assertEqual(
            count_scheduler.schedule_period_for(
                "Daily",
                None,
                datetime(2026, 8, 15, 23, 30, tzinfo=timezone.utc),
            ),
            "2026-08-16",
        )

    def test_selected_weekdays_is_due_only_on_configured_day(self):
        friday = datetime(2026, 8, 14, 9, 0)
        thursday = datetime(2026, 8, 13, 9, 0)
        self.assertEqual(count_scheduler.schedule_period_for("Selected Weekdays", "Fri", friday), "2026-08-14")
        self.assertIsNone(count_scheduler.schedule_period_for("Selected Weekdays", "Fri", thursday))

    def test_weekly_uses_configured_anchor_and_iso_period(self):
        monday_kl = datetime(2026, 8, 16, 16, 30, tzinfo=timezone.utc)
        tuesday_kl = datetime(2026, 8, 17, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(count_scheduler.schedule_period_for("Weekly", "Monday", monday_kl), "2026-W34")
        self.assertIsNone(count_scheduler.schedule_period_for("Weekly", "Monday", tuesday_kl))
        self.assertIsNone(count_scheduler.schedule_period_for("Weekly", "", monday_kl))

    def test_unconfigured_and_off_never_guess_a_due_period(self):
        self.assertIsNone(count_scheduler.effective_count_frequency(""))
        self.assertEqual(
            count_scheduler.effective_count_frequency("Off", ["inventory_high_use"]),
            "Off",
        )
        self.assertIsNone(count_scheduler.schedule_period_for("Weekly", "Monday,Tuesday"))

    def test_explicit_evidence_can_only_increase_cadence(self):
        self.assertEqual(
            count_scheduler.effective_count_frequency("Weekly", []),
            "Weekly",
        )
        self.assertEqual(
            count_scheduler.effective_count_frequency("Weekly", ["inventory_high_use"]),
            "Daily",
        )
        self.assertEqual(
            count_scheduler.effective_count_frequency("Selected Weekdays", ["inventory_short_life"]),
            "Daily",
        )
        self.assertEqual(
            count_scheduler.effective_count_frequency("Daily", ["inventory_projection_failed"]),
            "Daily",
        )
        self.assertEqual(
            count_scheduler.effective_count_frequency("Off", ["inventory_projection_dead_letter"]),
            "Off",
        )

    def test_no_active_items_does_not_create_a_task(self):
        policy = {"company": "JiJi", "warehouse": "Outlet - JIJI", "name": "POLICY-1"}
        group = {"name": "Ingredients", "custom_kopos_count_frequency": "Daily"}
        with patch.object(count_scheduler, "_active_items", return_value=[]), patch.object(
            count_scheduler, "_stock_ledger_watermark"
        ) as watermark:
            result = count_scheduler._schedule_for_group(policy=policy, group=group, at=datetime(2026, 8, 14))

        self.assertEqual(result["status"], "no_active_items")
        watermark.assert_not_called()

    def test_active_group_with_blank_frequency_is_reported_not_omitted(self):
        policy = {"company": "JiJi", "warehouse": "Outlet - JIJI", "name": "POLICY-1"}
        group = {"name": "Ingredients", "custom_kopos_count_frequency": ""}
        with patch.object(count_scheduler, "_active_items", return_value=[{"name": "MILK", "stock_uom": "Litre", "purchase_uom": "Litre"}]), patch.object(
            count_scheduler, "upsert_inventory_exception", return_value="EX-1"
        ):
            result = count_scheduler._schedule_for_group(policy=policy, group=group, at=datetime(2026, 8, 14))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "inventory_count_schedule_unconfigured")

    def test_invalid_weekly_configuration_is_blocked_without_guessing_anchor_day(self):
        policy = {"company": "JiJi", "warehouse": "Outlet - JIJI", "name": "POLICY-1"}
        group = {
            "name": "Ingredients",
            "custom_kopos_count_frequency": "Weekly",
            "custom_kopos_count_weekdays": "Monday,Tuesday",
        }
        with patch.object(count_scheduler, "_active_items", return_value=[{"name": "MILK", "stock_uom": "Litre", "purchase_uom": "Litre"}]), patch.object(
            count_scheduler, "upsert_inventory_exception", return_value="EX-1"
        ):
            result = count_scheduler._schedule_for_group(policy=policy, group=group, at=datetime(2026, 8, 14))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "inventory_count_schedule_unconfigured")

    def test_task_uses_exact_stock_uom_and_ledger_watermark(self):
        policy = {"company": "JiJi", "warehouse": "Outlet - JIJI", "name": "POLICY-1"}
        group = {"name": "Ingredients", "custom_kopos_count_frequency": "Daily"}
        items = [
            {"name": "COFFEE-BEAN", "stock_uom": "Gram", "purchase_uom": "Gram"},
            {"name": "MILK", "stock_uom": "Millilitre", "purchase_uom": "Millilitre"},
        ]
        with patch.object(count_scheduler, "_active_items", return_value=items), patch.object(
            count_scheduler, "_cadence_evidence", return_value=()
        ), patch.object(
            count_scheduler, "_stock_ledger_watermark", return_value="2026-08-14 08:00:00"
        ), patch.object(count_scheduler, "_task_by_schedule_key", return_value=None), patch.object(
            count_scheduler, "_create_count_task", return_value="INV-COUNT-1"
        ) as create:
            result = count_scheduler._schedule_for_group(policy=policy, group=group, at=datetime(2026, 8, 14))

        self.assertEqual(result["status"], "created")
        kwargs = create.call_args.kwargs
        self.assertEqual(kwargs["watermark"], "2026-08-14 08:00:00")
        self.assertEqual(kwargs["items"], items)
        self.assertEqual(kwargs["frequency"], "Daily")
        self.assertEqual(kwargs["period"], "2026-08-14")

    def test_existing_schedule_key_is_idempotent(self):
        policy = {"company": "JiJi", "warehouse": "Outlet - JIJI", "name": "POLICY-1"}
        group = {"name": "Ingredients", "custom_kopos_count_frequency": "Daily"}
        with patch.object(count_scheduler, "_active_items", return_value=[{"name": "MILK", "stock_uom": "Litre", "purchase_uom": "Litre"}]), patch.object(
            count_scheduler, "_cadence_evidence", return_value=()
        ), patch.object(count_scheduler, "_stock_ledger_watermark", return_value="watermark"), patch.object(
            count_scheduler, "_task_by_schedule_key", return_value="INV-COUNT-EXISTING"
        ), patch.object(count_scheduler, "_create_count_task") as create:
            result = count_scheduler._schedule_for_group(policy=policy, group=group, at=datetime(2026, 8, 14))

        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["task"], "INV-COUNT-EXISTING")
        create.assert_not_called()

    def test_count_task_document_contains_only_operational_lines(self):
        document = _TaskDocument()
        with patch.object(count_scheduler.frappe, "new_doc", return_value=document):
            result = count_scheduler._create_count_task(
                company="JiJi",
                warehouse="Outlet - JIJI",
                stock_group="Ingredients",
                frequency="Daily",
                period="2026-08-14",
                schedule_key="IC-key",
                watermark="watermark",
                items=[{"name": "MILK", "stock_uom": "Litre", "purchase_uom": "Litre", "conversion_factor": "1"}],
            )

        self.assertEqual(result, "INV-COUNT-00001")
        self.assertEqual(document.lines, [{
            "item_id": "MILK",
            "uom": "Litre",
            "stock_uom": "Litre",
            "purchase_uom": "Litre",
            "conversion_factor": "1",
        }])
        self.assertEqual(document.status, "Assigned")
        self.assertEqual(document.revision, 1)
        self.assertEqual(document.stock_watermark, "watermark")
        self.assertTrue(document.inserted)
        self.assertFalse(hasattr(document, "valuation"))
        self.assertFalse(hasattr(document, "cogs"))

    def test_missing_purchase_conversion_blocks_assignment(self):
        policy = {"company": "JiJi", "warehouse": "Outlet - JIJI", "name": "POLICY-1"}
        group = {"name": "Ingredients", "custom_kopos_count_frequency": "Daily"}
        with patch.object(
            count_scheduler,
            "_active_items",
            return_value=[{"name": "MILK", "stock_uom": "Litre", "purchase_uom": "Carton"}],
        ), patch.object(count_scheduler, "upsert_inventory_exception", return_value="EX-1") as exception:
            result = count_scheduler._schedule_for_group(policy=policy, group=group, at=datetime(2026, 8, 14))

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason_code"], "inventory_count_missing_uom_conversion")
        exception.assert_called_once()

    def test_purchase_conversion_is_frozen_as_decimal_text(self):
        item = {"name": "MILK", "stock_uom": "Litre", "purchase_uom": "Carton"}
        with patch.object(count_scheduler.frappe.db, "get_value", return_value="12.5000"):
            self.assertIsNone(count_scheduler._count_uom_authority_error(item))
        self.assertEqual(item["conversion_factor"], "12.5")
