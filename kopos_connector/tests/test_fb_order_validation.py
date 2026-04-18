from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


class TestFBOrderModifierValidation(unittest.TestCase):
    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_cached_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_selected_modifier_uses_fb_modifier_record_values(
        self, mock_exists, mock_get_cached_doc
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_selected_modifier

        existing_docs = {
            ("FB Modifier Group", "FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"),
        }
        mock_exists.side_effect = lambda doctype, name: (doctype, name) in existing_docs
        mock_get_cached_doc.side_effect = lambda doctype, name: {
            ("FB Modifier Group", "FB-GRP-TEMP"): MagicMock(name="FB-GRP-TEMP"),
            (
                "FB Modifier",
                "FB-MOD-ICED",
            ): MagicMock(
                name="FB-MOD-ICED",
                modifier_group="FB-GRP-TEMP",
                price_adjustment=1.5,
                instruction_text="Less ice",
                display_order=4,
                affects_stock=1,
                affects_recipe=0,
            ),
        }[(doctype, name)]

        result = _validate_selected_modifier(
            {
                "modifier_group": "FB-GRP-TEMP",
                "modifier": "FB-MOD-ICED",
                "price_adjustment": 99,
                "instruction_text": "",
                "sort_order": 0,
                "affects_stock": 0,
                "affects_recipe": 1,
            },
            1,
            1,
        )

        self.assertEqual(result["modifier_group"], "FB-GRP-TEMP")
        self.assertEqual(result["modifier"], "FB-MOD-ICED")
        self.assertEqual(result["price_adjustment"], 1.5)
        self.assertEqual(result["instruction_text"], "Less ice")
        self.assertEqual(result["sort_order"], 4)
        self.assertEqual(result["affects_stock"], 1)
        self.assertEqual(result["affects_recipe"], 0)

    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_selected_modifier_rejects_legacy_kopos_modifier_id(
        self, mock_exists
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_selected_modifier

        mock_exists.return_value = True

        with self.assertRaises(Exception) as context:
            _validate_selected_modifier(
                {
                    "modifier_group": "FB-GRP-TEMP",
                    "modifier": "KOPOS-OPT-00001",
                },
                1,
                1,
            )

        self.assertIn("legacy KoPOS modifier id", str(context.exception))
        self.assertIn("FB-only modifier ids", str(context.exception))

    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_cached_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_order_item_requires_modifier_total_to_match_fb_prices(
        self, mock_exists, mock_get_cached_doc, mock_get_doc
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_order_item

        existing_docs = {
            ("UOM", "Nos"),
            ("FB Modifier Group", "FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"),
        }
        mock_exists.side_effect = lambda doctype, name: (doctype, name) in existing_docs
        mock_get_doc.return_value = MagicMock(
            name="ITEM-COFFEE", item_name="Coffee", stock_uom="Nos"
        )
        mock_get_cached_doc.side_effect = lambda doctype, name: {
            ("FB Modifier Group", "FB-GRP-TEMP"): MagicMock(name="FB-GRP-TEMP"),
            (
                "FB Modifier",
                "FB-MOD-ICED",
            ): MagicMock(
                name="FB-MOD-ICED",
                modifier_group="FB-GRP-TEMP",
                price_adjustment=1.5,
                instruction_text=None,
                display_order=2,
                affects_stock=1,
                affects_recipe=0,
            ),
        }[(doctype, name)]

        with self.assertRaises(Exception) as context:
            _validate_order_item(
                {
                    "item_code": "ITEM-COFFEE",
                    "qty": 1,
                    "uom": "Nos",
                    "unit_price": 10,
                    "modifier_total": 0,
                    "discount_amount": 0,
                    "line_total": 10,
                    "selected_modifiers": [
                        {
                            "modifier_group": "FB-GRP-TEMP",
                            "modifier": "FB-MOD-ICED",
                        }
                    ],
                },
                1,
            )

        self.assertIn(
            "modifier_total must equal summed FB modifier price adjustments",
            str(context.exception),
        )

    @patch("kopos_connector.kopos.api.fb_orders.frappe.get_cached_doc")
    @patch("kopos_connector.kopos.api.fb_orders.frappe.db.exists")
    def test_validate_selected_modifier_rejects_fb_modifier_from_another_group(
        self, mock_exists, mock_get_cached_doc
    ):
        from kopos_connector.kopos.api.fb_orders import _validate_selected_modifier

        existing_docs = {
            ("FB Modifier Group", "FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"),
        }
        mock_exists.side_effect = lambda doctype, name: (doctype, name) in existing_docs
        mock_get_cached_doc.side_effect = lambda doctype, name: {
            ("FB Modifier Group", "FB-GRP-TEMP"): MagicMock(name="FB-GRP-TEMP"),
            ("FB Modifier", "FB-MOD-ICED"): MagicMock(
                name="FB-MOD-ICED",
                modifier_group="FB-GRP-OTHER",
                price_adjustment=0,
                instruction_text=None,
                display_order=1,
                affects_stock=0,
                affects_recipe=0,
            ),
        }[(doctype, name)]

        with self.assertRaises(Exception) as context:
            _validate_selected_modifier(
                {
                    "modifier_group": "FB-GRP-TEMP",
                    "modifier": "FB-MOD-ICED",
                },
                1,
                1,
            )

        self.assertIn("does not belong to FB Modifier Group", str(context.exception))


if __name__ == "__main__":
    unittest.main()
