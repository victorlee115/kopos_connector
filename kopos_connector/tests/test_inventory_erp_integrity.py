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
