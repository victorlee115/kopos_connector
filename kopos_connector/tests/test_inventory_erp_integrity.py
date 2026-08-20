"""Regressions for the ERP integrity findings in the 2026-08-19 vetting report.

Evidence level: static + mocked_unit. These run against
``kopos_connector.tests.fake_frappe``; a green run does not mean MariaDB wrote a
Stock Ledger Entry or a GL Entry.
"""

from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.inventory import waste_service
from kopos_connector.kopos.services.inventory_autopilot import menu_authoring

ERP_ROOT = Path(__file__).resolve().parents[1]


class WasteIdempotencyTests(TestCase):
    def test_waste_command_id_is_derived_from_the_event(self) -> None:
        self.assertEqual(waste_service.build_waste_command_id("WASTE-0001"), "fb-waste:WASTE-0001")
        self.assertEqual(
            waste_service.build_waste_command_id("  WASTE-0001  "),
            waste_service.build_waste_command_id("WASTE-0001"),
        )

    def test_waste_command_id_requires_an_event_identity(self) -> None:
        for value in ("", "   "):
            with self.assertRaises(Exception):
                waste_service.build_waste_command_id(value)

    def test_waste_quantity_prefers_exact_decimal_text(self) -> None:
        from decimal import Decimal

        self.assertEqual(
            waste_service._decimal_quantity({"qty": 0.1, "qty_decimal": "0.1"}),
            Decimal("0.1"),
        )

    def test_waste_quantity_falls_back_to_the_legacy_float_column(self) -> None:
        from decimal import Decimal

        self.assertEqual(waste_service._decimal_quantity({"qty": "2.5"}), Decimal("2.5"))

    def test_waste_quantity_refuses_zero_negative_and_malformed(self) -> None:
        for line in ({"qty": "0"}, {"qty": "-1"}, {"qty": "abc"}, {"qty": ""}):
            with self.assertRaises(Exception):
                waste_service._decimal_quantity(line)


class SaleableItemClassificationTests(TestCase):
    def test_a_saleable_item_need_not_be_a_drink(self) -> None:
        # Keychains and the event booth are sold but are not made-to-order drinks.
        self.assertIn("Packaged Finished Good", menu_authoring.SALEABLE_ITEM_ROLES)
        self.assertIn("Non-Stock Service", menu_authoring.SALEABLE_ITEM_ROLES)

    def test_only_a_drink_carries_the_recipe_requirement(self) -> None:
        self.assertEqual(menu_authoring.DRINK_RECIPE_ROLE, "Sellable Drink")
        self.assertIn(menu_authoring.DRINK_RECIPE_ROLE, menu_authoring.SALEABLE_ITEM_ROLES)

    def test_component_roles_are_not_saleable_classifications(self) -> None:
        for role in ("Ingredient", "Prep Item", "Packaging", "Tool"):
            self.assertNotIn(role, menu_authoring.SALEABLE_ITEM_ROLES)

    def test_classification_dialog_offers_every_saleable_role(self) -> None:
        page = (ERP_ROOT / "kopos" / "page" / "jiji_menu_recipes" / "jiji_menu_recipes.js").read_text()
        for role in sorted(menu_authoring.SALEABLE_ITEM_ROLES):
            self.assertIn(role, page)


class ReturnToStockTests(TestCase):
    def test_refund_restock_fails_closed(self) -> None:
        from kopos_connector.kopos.services.inventory import stock_reversal_service

        with self.assertRaises(Exception):
            stock_reversal_service.create_reversal_stock_entry({"return_to_stock": 1})

    def test_restock_never_returns_a_stock_entry_for_any_input(self) -> None:
        from kopos_connector.kopos.services.inventory import stock_reversal_service

        # Asserted behaviourally rather than by grepping the source: the point is
        # that no input reaches stock, not that a particular line is absent.
        for payload in (
            None,
            {},
            {"return_to_stock": 0},
            {"return_to_stock": 1, "lines": []},
            {"return_to_stock": 1, "lines": [{"original_resolved_sale": "RS-1", "qty_returned": 2}]},
        ):
            with self.assertRaises(Exception):
                stock_reversal_service.create_reversal_stock_entry(payload)


class DeskPagePackagingTests(TestCase):
    """Frappe loads a Page's assets from <app>/<module_dir>/page/<page_name>/.

    ``frappe/core/doctype/page/page.py::load_assets`` does ``os.listdir`` on
    exactly that path. Both JiJi pages declare ``module: KoPOS``, whose directory
    is ``kopos_connector/kopos``. The assets previously sat in
    ``kopos_connector/page/``, which is not a module directory, so the desk pages
    could never load their JS: with an empty directory at the module path Frappe
    returned a blank page, and with no directory at all it raised
    FileNotFoundError and the page 500ed.
    """

    PAGES = ("jiji_stock_autopilot", "jiji_menu_recipes")

    def test_page_assets_live_where_frappe_loads_them(self) -> None:
        for page in self.PAGES:
            module_path = ERP_ROOT / "kopos" / "page" / page
            self.assertTrue(
                module_path.is_dir(),
                f"{page} must exist under the KoPOS module directory or Frappe raises"
                " FileNotFoundError when loading the desk page",
            )
            self.assertTrue(
                (module_path / f"{page}.js").is_file(),
                f"{page}.js must sit beside its page directory or the desk page renders blank",
            )
            self.assertTrue(
                (module_path / f"{page}.json").is_file(),
                f"{page}.json must sit beside its page directory",
            )

    def test_page_assets_are_not_left_outside_the_module(self) -> None:
        for page in self.PAGES:
            self.assertFalse(
                (ERP_ROOT / "page" / page).exists(),
                f"{page} must not also live outside the module directory; two copies"
                " is what made the authoritative location ambiguous",
            )


class WireTimestampStorageTests(TestCase):
    """A Frappe Datetime column is a site-local *naive* MariaDB DATETIME.

    The tablet sends offset-aware ISO-8601 (``2026-08-20T05:23:24.910Z``). Handing
    that string straight to a Datetime field makes MariaDB raise error 1292 and
    discard the whole write. On the review stack this rejected the device
    inventory report 263 times without a single success, so ``inventory_observed_at``
    was never populated and every check reading it was blind. ``fake_frappe`` does
    not enforce column types, which is exactly why the host suite stayed green.
    """

    WIRE_VALUES = (
        "2026-08-20T05:23:24.910Z",
        "2026-08-20T13:23:24.910+08:00",
        "2026-08-19T21:23:24.910-08:00",
    )

    def test_a_wire_timestamp_becomes_naive_before_it_reaches_a_datetime_column(self) -> None:
        from kopos_connector.api import inventory

        for wire in self.WIRE_VALUES:
            stored = inventory._site_local_naive(
                inventory._require_explicit_offset_timestamp(wire, "test")
            )
            self.assertIsNone(
                stored.tzinfo,
                f"{wire} still carries an offset; MariaDB rejects it with error 1292",
            )
            self.assertNotIn("Z", stored.isoformat())
            self.assertNotIn("+", stored.isoformat())

    def test_the_instant_survives_the_conversion(self) -> None:
        from datetime import timezone
        from zoneinfo import ZoneInfo

        from kopos_connector.api import inventory

        # fake_frappe reports Asia/Kuala_Lumpur (UTC+8), so 05:23Z is 13:23 local.
        stored = inventory._site_local_naive(
            inventory._require_explicit_offset_timestamp("2026-08-20T05:23:24.910Z", "test")
        )
        self.assertEqual(stored.hour, 13)
        self.assertEqual(
            stored.replace(tzinfo=ZoneInfo("Asia/Kuala_Lumpur")).astimezone(timezone.utc).hour,
            5,
        )

    def test_the_same_instant_in_any_offset_stores_identically(self) -> None:
        from kopos_connector.api import inventory

        stored = {
            inventory._site_local_naive(
                inventory._require_explicit_offset_timestamp(wire, "test")
            )
            for wire in self.WIRE_VALUES
        }
        self.assertEqual(len(stored), 1, "the same instant must land on one site-local value")

    def test_a_naive_value_is_left_alone_and_none_passes_through(self) -> None:
        from datetime import datetime

        from kopos_connector.api import inventory

        naive = datetime(2026, 8, 20, 13, 23, 24)
        self.assertEqual(inventory._site_local_naive(naive), naive)
        self.assertIsNone(inventory._site_local_naive(None))


class WireTimestampCallSiteTests(TestCase):
    """Pin the call sites structurally, via AST rather than text.

    A previous source-grep guard in this file matched its own docstring. Reading
    the syntax tree means only a real assignment expression can satisfy these.
    """

    @staticmethod
    def _module() -> "ast.Module":
        import ast

        return ast.parse((ERP_ROOT / "api" / "inventory.py").read_text())

    @staticmethod
    def _calls_coercion(node: "ast.AST") -> bool:
        import ast

        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                if inner.func.id == "_site_local_naive":
                    return True
        return False

    def test_device_report_datetime_columns_are_coerced(self) -> None:
        import ast

        module = self._module()
        checked = 0
        for node in ast.walk(module):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                if key.value not in {"inventory_observed_at", "inventory_oldest_unsaved_sale_at"}:
                    continue
                checked += 1
                self.assertTrue(
                    self._calls_coercion(value),
                    f"{key.value} is a Frappe Datetime column and must be written through"
                    " _site_local_naive; a raw wire string makes MariaDB reject the write",
                )
        self.assertEqual(checked, 2, "both device Datetime columns must be covered")

    def test_count_observation_observed_at_is_coerced(self) -> None:
        import ast

        module = self._module()
        checked = 0
        for node in ast.walk(module):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "observed_at"
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "observation_doc"
                ):
                    checked += 1
                    self.assertTrue(
                        self._calls_coercion(node.value),
                        "observation_doc.observed_at is a Datetime column and must be coerced",
                    )
        self.assertEqual(checked, 1)

    def test_reconciliation_posts_on_the_site_local_day(self) -> None:
        import ast

        module = self._module()
        seen = set()
        for node in ast.walk(module):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"posting_date", "posting_time"}
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "reconciliation"
                ):
                    seen.add(target.attr)
                    # Must derive from a coerced value, not from the sender's offset:
                    # .date() on a +08:00 instant sent as UTC lands a 23:30 count
                    # on the previous accounting day.
                    self.assertTrue(
                        isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Attribute)
                        and isinstance(node.value.func.value, ast.Name)
                        and node.value.func.value.id == "posted_at",
                        f"reconciliation.{target.attr} must come from the site-local instant",
                    )
        self.assertEqual(seen, {"posting_date", "posting_time"})


class GuidedTaskEndpointRegistrationTests(TestCase):
    """Every guided inventory task needs a *reachable* ERP endpoint.

    The tablet calls ``kopos_connector.api.<task_type>``. Frappe resolves that
    against ``kopos_connector/api/__init__.py``, so a handler defined in
    ``api/inventory.py`` but never re-exported does not exist as far as the
    device is concerned: the request fails with HTTP 417 "Failed to get method"
    before the handler runs.

    ``record_waste_event`` was defined, whitelisted, and wired into the tablet UI,
    but never exported. It was the last of four registration points the waste
    capability was missing from (the other three were on the POS side: the outbox
    dispatcher and the client allowlist). Walking the task list here means a new
    task type cannot ship half-registered.
    """

    TASK_TYPES = (
        "confirm_count_reconciliation",
        "accept_preparation_task",
        "start_preparation_task",
        "complete_preparation_task",
        "submit_purchase_receipt",
        "submit_transfer_dispatch",
        "submit_transfer_receipt",
        "record_waste_event",
    )

    def test_every_guided_task_handler_is_defined(self) -> None:
        source = (ERP_ROOT / "api" / "inventory.py").read_text()
        for task_type in self.TASK_TYPES:
            self.assertIn(
                f"def {task_type}(",
                source,
                f"{task_type} has no handler in api/inventory.py",
            )

    def test_every_guided_task_endpoint_is_exported(self) -> None:
        import ast

        module = ast.parse((ERP_ROOT / "api" / "__init__.py").read_text())

        imported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    imported.add(alias.asname or alias.name)

        exported: set[str] = set()
        for node in ast.walk(module):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ):
                for element in getattr(node.value, "elts", []):
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        exported.add(element.value)

        for task_type in self.TASK_TYPES:
            self.assertIn(
                task_type,
                imported,
                f"kopos_connector.api.{task_type} is not importable, so Frappe answers"
                " 417 'Failed to get method' and the device can never submit it",
            )
            self.assertIn(
                task_type,
                exported,
                f"{task_type} is missing from __all__",
            )
