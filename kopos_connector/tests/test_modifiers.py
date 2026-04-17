# Copyright (c) 2026, KoPOS and contributors
# For license information, please see license.txt

import unittest
from unittest.mock import MagicMock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()

# Mock jsonschema if not installed
try:
    import jsonschema
except ImportError:
    import sys

    jsonschema_mock = MagicMock()
    jsonschema_mock.validate = MagicMock()
    jsonschema_mock.ValidationError = Exception
    sys.modules["jsonschema"] = jsonschema_mock


class TestModifierValidation(unittest.TestCase):
    """Unit tests for modifier validation logic."""

    def test_sanitize_modifier_text_removes_dangerous_patterns(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        self.assertEqual(sanitize_modifier_text("Normal text"), "Normal text")
        # Dangerous patterns are replaced with [removed] but surrounding text remains
        result = sanitize_modifier_text("javascript:alert(1)")
        self.assertIn("[removed]", result)
        self.assertNotIn("javascript:", result)
        result = sanitize_modifier_text("vbscript:msgbox(1)")
        self.assertIn("[removed]", result)
        self.assertNotIn("vbscript:", result)
        result = sanitize_modifier_text("data:text/html,<script>")
        self.assertIn("[removed]", result)
        self.assertNotIn("data:", result)
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

    def test_sanitize_modifier_text_handles_empty_string(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        self.assertEqual(sanitize_modifier_text(""), "")
        self.assertEqual(sanitize_modifier_text(None), "")

    def test_sanitize_modifier_text_truncates_long_input(self):
        from kopos_connector.api.modifiers import sanitize_modifier_text

        long_text = "A" * 500
        result = sanitize_modifier_text(long_text)
        self.assertLessEqual(len(result), 255)


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
        self.assertEqual(snapshot["version"], "1.0")

    def test_build_snapshot_handles_none_modifiers(self):
        from kopos_connector.api.modifiers import build_modifiers_snapshot

        raw_item = {"modifiers": None, "modifier_total": 0}
        snapshot = build_modifiers_snapshot(raw_item)

        self.assertEqual(snapshot["count"], 0)
        self.assertEqual(snapshot["modifiers"], [])

    def test_build_snapshot_raises_on_non_dict(self):
        from kopos_connector.api.modifiers import build_modifiers_snapshot

        with self.assertRaises(TypeError):
            build_modifiers_snapshot("not a dict")


class TestModifierTotalValidation(unittest.TestCase):
    """Unit tests for modifier total validation."""

    def test_validate_totals_returns_snapshot(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        snapshot = {
            "modifiers": [{"price": 100}, {"price": 50}],
            "total": 150,
            "count": 2,
            "version": "1.0",
        }

        result = validate_modifier_totals(snapshot)
        self.assertEqual(result["total"], 150)

    def test_validate_totals_corrects_mismatch(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        snapshot = {
            "modifiers": [{"price": 100}, {"price": 50}],
            "total": 200,
            "count": 2,
            "version": "1.0",
        }

        result = validate_modifier_totals(snapshot)
        self.assertEqual(result["total"], 150)

    def test_validate_totals_passes_within_tolerance(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        snapshot = {
            "modifiers": [{"price": 100}, {"price": 50}],
            "total": 150.005,
            "count": 2,
            "version": "1.0",
        }

        result = validate_modifier_totals(snapshot)
        self.assertEqual(result["total"], 150.005)

    def test_validate_totals_handles_empty_modifiers(self):
        from kopos_connector.api.modifiers import validate_modifier_totals

        snapshot = {"modifiers": [], "total": 0, "count": 0, "version": "1.0"}

        result = validate_modifier_totals(snapshot)
        self.assertEqual(result["total"], 0)


class TestBatchResolveLinks(unittest.TestCase):
    """Unit tests for batch link resolution."""

    @patch("frappe.db.get_all")
    def test_batch_resolve_returns_existing_links(self, mock_get_all):
        from kopos_connector.api.modifiers import _batch_resolve_links

        mock_get_all.side_effect = [
            ["group-1", "group-2"],
            ["opt-1", "opt-2"],
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

    def test_batch_resolve_handles_empty_modifiers(self):
        from kopos_connector.api.modifiers import _batch_resolve_links

        result = _batch_resolve_links([])

        self.assertEqual(result["groups"], set())
        self.assertEqual(result["options"], set())


class TestExtractModifiersFromDescription(unittest.TestCase):
    """Unit tests for legacy description parsing."""

    def test_extracts_modifiers_from_description(self):
        from kopos_connector.api.modifier_migration import (
            extract_modifiers_from_description,
        )

        description = "Iced Latte\n\nModifiers:\n- Oat Milk (+0.50)\n- Large (+1.00)"
        result = extract_modifiers_from_description(description)

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "Oat Milk")
        self.assertEqual(result[0]["price"], 0.50)
        self.assertEqual(result[1]["name"], "Large")
        self.assertEqual(result[1]["price"], 1.00)

    def test_returns_empty_for_no_modifiers(self):
        from kopos_connector.api.modifier_migration import (
            extract_modifiers_from_description,
        )

        description = "Iced Latte\nNo modifiers here"
        result = extract_modifiers_from_description(description)
        self.assertEqual(result, [])

    def test_returns_empty_for_none(self):
        from kopos_connector.api.modifier_migration import (
            extract_modifiers_from_description,
        )

        result = extract_modifiers_from_description(None)
        self.assertEqual(result, [])

    def test_handles_negative_prices(self):
        from kopos_connector.api.modifier_migration import (
            extract_modifiers_from_description,
        )

        description = "Item\n\nModifiers:\n- Discount (-1.00)"
        result = extract_modifiers_from_description(description)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["price"], -1.00)

    def test_ignores_parenthetical_text_before_modifiers(self):
        """Ensure text before 'Modifiers:' is not matched."""
        from kopos_connector.api.modifier_migration import (
            extract_modifiers_from_description,
        )

        description = "Large Item (freshly made)\n\nModifiers:\n- Extra Sauce (+2.00)"
        result = extract_modifiers_from_description(description)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Extra Sauce")
        self.assertEqual(result[0]["price"], 2.00)


class TestCatalogModifierGroups(unittest.TestCase):
    @patch("kopos_connector.api.catalog.frappe.get_all")
    def test_get_modifier_groups_includes_parent_option_id(self, mock_get_all):
        from kopos_connector.api.catalog import get_modifier_groups

        mock_get_all.return_value = [
            {
                "id": "grp-temp",
                "name": "Temperature",
                "selection_type": "single",
                "is_required": 1,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
                "parent_option_id": None,
            },
            {
                "id": "grp-ice",
                "name": "Ice Level",
                "selection_type": "single",
                "is_required": 1,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 2,
                "parent_option_id": "opt-iced",
            },
        ]

        result = get_modifier_groups()

        self.assertEqual(len(result), 2)
        self.assertIsNone(result[0]["parent_option_id"])
        self.assertEqual(result[1]["parent_option_id"], "opt-iced")

    @patch("kopos_connector.api.catalog.frappe.get_all")
    def test_get_modifier_groups_handles_missing_parent_option_id(self, mock_get_all):
        from kopos_connector.api.catalog import get_modifier_groups

        mock_get_all.return_value = [
            {
                "id": "grp-size",
                "name": "Size",
                "selection_type": "single",
                "is_required": 1,
                "min_selections": 0,
                "max_selections": 1,
                "display_order": 1,
            },
        ]

        result = get_modifier_groups()

        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].get("parent_option_id"))

    @patch("kopos_connector.api.catalog.frappe.get_all")
    def test_get_modifier_groups_with_since_filter(self, mock_get_all):
        from kopos_connector.api.catalog import get_modifier_groups

        mock_get_all.return_value = []

        get_modifier_groups(since="2026-03-01T00:00:00")

        mock_get_all.assert_called_once()
        call_filters = mock_get_all.call_args[1]["filters"]
        self.assertEqual(call_filters["is_active"], 1)
        self.assertEqual(call_filters["modified"], [">=", "2026-03-01T00:00:00"])

    @patch("kopos_connector.api.catalog.frappe.get_all")
    def test_get_modifier_groups_returns_all_required_fields(self, mock_get_all):
        from kopos_connector.api.catalog import get_modifier_groups

        mock_get_all.return_value = [
            {
                "id": "grp-test",
                "name": "Test Group",
                "selection_type": "multiple",
                "is_required": 1,
                "min_selections": 2,
                "max_selections": 5,
                "display_order": 10,
                "parent_option_id": "opt-parent",
            },
        ]

        result = get_modifier_groups()

        self.assertEqual(len(result), 1)
        group = result[0]
        self.assertEqual(group["id"], "grp-test")
        self.assertEqual(group["name"], "Test Group")
        self.assertEqual(group["selection_type"], "multiple")
        self.assertEqual(group["is_required"], 1)
        self.assertEqual(group["min_selections"], 2)
        self.assertEqual(group["max_selections"], 5)
        self.assertEqual(group["display_order"], 10)
        self.assertEqual(group["parent_option_id"], "opt-parent")


class TestModifierOptionChoices(unittest.TestCase):
    @patch("kopos_connector.api.catalog.get_modifier_options")
    @patch("kopos_connector.api.catalog.get_session_roles")
    def test_list_modifier_option_choices_for_system_manager(
        self, mock_get_session_roles, mock_get_modifier_options
    ):
        from kopos_connector.api.catalog import list_modifier_option_choices

        mock_get_session_roles.return_value = {"System Manager"}
        mock_get_modifier_options.return_value = [
            {"id": "KOPOS-OPT-00001", "name": "Iced", "group_id": "Temperature"},
            {"id": "KOPOS-OPT-00002", "name": "Hot", "group_id": "Temperature"},
        ]

        result = list_modifier_option_choices()

        self.assertEqual(
            result,
            [
                {"value": "KOPOS-OPT-00001", "label": "Iced (Temperature)"},
                {"value": "KOPOS-OPT-00002", "label": "Hot (Temperature)"},
            ],
        )

    @patch("kopos_connector.api.catalog.get_session_roles")
    def test_list_modifier_option_choices_rejects_unauthorized_user(
        self, mock_get_session_roles
    ):
        from kopos_connector.api.catalog import list_modifier_option_choices

        mock_get_session_roles.return_value = {"POS User"}

        with patch(
            "kopos_connector.api.catalog.frappe.session",
            MagicMock(user="cashier@example.com"),
        ):
            with self.assertRaises(Exception):
                list_modifier_option_choices()


class TestCatalogApiElevation(unittest.TestCase):
    def test_get_catalog_elevates_device_requests(self):
        from kopos_connector.api import get_catalog

        events = []

        class ElevationContext:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, exc_type, exc, tb):
                events.append("exit")

        with (
            patch(
                "kopos_connector.api.require_device_context"
            ) as require_device_context,
            patch("kopos_connector.api.mark_device_seen") as mark_device_seen,
            patch(
                "kopos_connector.api.elevate_device_api_user",
                return_value=ElevationContext(),
            ),
            patch(
                "kopos_connector.api.build_catalog_payload",
                return_value={"items": [], "modifier_groups": [], "categories": []},
            ) as build_catalog_payload,
            patch(
                "kopos_connector.api.frappe.local",
                MagicMock(response={}),
            ),
        ):
            get_catalog(device_id="device-1")

        require_device_context.assert_called_once_with(device_id="device-1")
        mark_device_seen.assert_called_once_with(device_id="device-1")
        build_catalog_payload.assert_called_once_with(since=None, device_id="device-1")
        self.assertEqual(events, ["enter", "exit"])

    def test_get_item_modifiers_elevates_device_requests(self):
        from kopos_connector.api import get_item_modifiers

        events = []

        class ElevationContext:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, exc_type, exc, tb):
                events.append("exit")

        with (
            patch(
                "kopos_connector.api.require_kopos_api_access"
            ) as require_kopos_api_access,
            patch(
                "kopos_connector.api.elevate_device_api_user",
                return_value=ElevationContext(),
            ),
            patch(
                "kopos_connector.api.get_item_modifiers_payload",
                return_value=[],
            ) as get_item_modifiers_payload,
            patch(
                "kopos_connector.api.frappe.local",
                MagicMock(response={}),
            ),
        ):
            get_item_modifiers("ITEM-1")

        require_kopos_api_access.assert_called_once_with()
        get_item_modifiers_payload.assert_called_once_with("ITEM-1")
        self.assertEqual(events, ["enter", "exit"])


if __name__ == "__main__":
    unittest.main()
