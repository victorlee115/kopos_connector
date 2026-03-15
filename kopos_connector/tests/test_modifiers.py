# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import unittest
from unittest.mock import MagicMock, patch


class TestModifierValidation(unittest.TestCase):
    """Unit tests for modifier validation logic."""

    def test_sanitize_modifier_text_removes_dangerous_patterns(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        self.assertEqual(sanitize_modifier_text("Normal text"), "Normal text")
        self.assertEqual(sanitize_modifier_text("javascript:alert(1)"), "")
        self.assertEqual(sanitize_modifier_text("vbscript:msgbox(1)"), "")
        self.assertEqual(sanitize_modifier_text("data:text/html,<script>"), "")
        self.assertIn("&lt;", sanitize_modifier_text("<script>alert(1)</script>"))

    def test_sanitize_modifier_text_normalizes_unicode(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        homograph_a = "\uff41"
        result = sanitize_modifier_text(homograph_a)
        self.assertEqual(result, "a")

    def test_sanitize_modifier_text_removes_control_chars(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        text_with_control = "Hello\x00World\x1f"
        result = sanitize_modifier_text(text_with_control)
        self.assertNotIn("\x00", result)
        self.assertNotIn("\x1f", result)

    def test_sanitize_modifier_text_preserves_newlines(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        text = "Line 1\nLine 2\r\nLine 3"
        result = sanitize_modifier_text(text)
        self.assertIn("\n", result)
        self.assertIn("\r", result)


class TestModifierSnapshot(unittest.TestCase):
    """Unit tests for snapshot building."""

    def test_build_snapshot_structure(self):
        from kopos_connector.api.modifiers import build_modifiers_snapshot

        raw_item = {
            "modifiers": [
                {
                    "id": "opt-1",
                    "name": "Large",
                    "group_id": "size",
                    "group_name": "Size",
                    "price": 100,
                }
            ]
        }

        snapshot = build_modifiers_snapshot(raw_item)

        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["total"], 100)
        self.assertEqual(len(snapshot["modifiers"]), 1)
        self.assertEqual(snapshot["modifiers"][0]["name"], "Large")

    def test_build_snapshot_calculates_total(self):
        from kopos_connector.api.modifiers import build_modifiers_snapshot

        raw_item = {
            "modifiers": [
                {
                    "id": "opt-1",
                    "name": "Large",
                    "group_id": "size",
                    "group_name": "Size",
                    "price": 100,
                },
                {
                    "id": "opt-2",
                    "name": "Oat Milk",
                    "group_id": "milk",
                    "group_name": "Milk",
                    "price": 50,
                },
            ]
        }

        snapshot = build_modifiers_snapshot(raw_item)

        self.assertEqual(snapshot["count"], 2)
        self.assertEqual(snapshot["total"], 150)

    def test_build_snapshot_handles_empty_modifiers(self):
        from kopos_connector.api.modifiers import build_modifiers_snapshot

        raw_item = {"modifiers": []}
        snapshot = build_modifiers_snapshot(raw_item)

        self.assertEqual(snapshot["count"], 0)
        self.assertEqual(snapshot["total"], 0)
        self.assertEqual(snapshot["modifiers"], [])

    def test_build_snapshot_includes_schema_version(self):
        from kopos_connector.api.modifiers import build_modifiers_snapshot

        raw_item = {
            "modifiers": [
                {
                    "id": "opt-1",
                    "name": "Test",
                    "group_id": "g1",
                    "group_name": "Group",
                    "price": 0,
                }
            ]
        }
        snapshot = build_modifiers_snapshot(raw_item)

        self.assertIn("version", snapshot)
        self.assertEqual(snapshot["version"], 1)


class TestModifierTotalValidation(unittest.TestCase):
    """Unit tests for modifier total validation."""

    def test_validate_totals_passes_when_matching(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        modifiers = [
            {"price": 100},
            {"price": 50},
        ]

        result = validate_modifier_totals(modifiers, 150)
        self.assertTrue(result)

    def test_validate_totals_passes_within_tolerance(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        modifiers = [
            {"price": 100},
            {"price": 50},
        ]

        result = validate_modifier_totals(modifiers, 150.005)
        self.assertTrue(result)

    def test_validate_totals_raises_on_mismatch(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        modifiers = [
            {"price": 100},
            {"price": 50},
        ]

        with self.assertRaises(ValueError):
            validate_modifier_totals(modifiers, 200)


class TestBatchResolveLinks(unittest.TestCase):
    """Unit tests for batch link resolution."""

    @patch("frappe.db.get_all")
    def test_batch_resolve_returns_existing_links(self, mock_get_all):
        from kopos_connector.api.modifiers import _batch_resolve_links

        mock_get_all.side_effect = [
            [{"name": "group-1"}, {"name": "group-2"}],
            [{"name": "opt-1"}, {"name": "opt-2"}],
        ]

        modifiers = [
            {"group_id": "group-1", "id": "opt-1"},
            {"group_id": "group-2", "id": "opt-2"},
            {"group_id": "group-missing", "id": "opt-missing"},
        ]

        result = _batch_resolve_links(modifiers)

        self.assertIn("group-1", result["groups"])
        self.assertIn("group-2", result["groups"])
        self.assertNotIn("group-missing", result["groups"])
        self.assertIn("opt-1", result["options"])
        self.assertIn("opt-2", result["options"])
        self.assertNotIn("opt-missing", result["options"])


class TestDeviceTypeDetection(unittest.TestCase):
    """Test mobile theme breakpoints."""

    def test_phone_detection(self):
        from kopos_connector.mobile.theme.breakpoints import getDeviceType, BREAKPOINTS

        self.assertEqual(getDeviceType(400), "phone")
        self.assertEqual(getDeviceType(500), "phoneLarge")

    def test_tablet_detection(self):
        from kopos_connector.mobile.theme.breakpoints import getDeviceType, isTablet

        self.assertEqual(getDeviceType(800), "tabletSmall")
        self.assertEqual(getDeviceType(950), "tablet")
        self.assertEqual(getDeviceType(1100), "tabletLarge")

        self.assertTrue(isTablet(700))
        self.assertTrue(isTablet(800))
        self.assertFalse(isTablet(600))


if __name__ == "__main__":
    unittest.main()
