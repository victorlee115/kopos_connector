from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import cast, TypedDict
from unittest.mock import MagicMock, patch

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules

install_fake_frappe_modules()


class LegacyOption(TypedDict):
    legacy_id: str
    option_name: str
    price_adjustment: float
    is_default: int
    is_active: int
    display_order: int


class LegacyGroup(TypedDict):
    legacy_id: str
    group_name: str
    selection_type: str
    is_required: int
    min_selections: int
    max_selections: int
    display_order: int
    is_active: int
    parent_option_id: str | None
    default_resolution_policy: str
    options: list[LegacyOption]


def _legacy_group(
    legacy_id: str,
    group_name: str,
    selection_type: str,
    is_required: int,
    min_selections: int,
    max_selections: int,
    display_order: int,
    is_active: int,
    parent_option_id: str | None,
    options: list[LegacyOption],
) -> LegacyGroup:
    return {
        "legacy_id": legacy_id,
        "group_name": group_name,
        "selection_type": selection_type,
        "is_required": is_required,
        "min_selections": min_selections,
        "max_selections": max_selections,
        "display_order": display_order,
        "is_active": is_active,
        "parent_option_id": parent_option_id,
        "default_resolution_policy": (
            "Auto Apply Default"
            if any(option["is_default"] and option["is_active"] for option in options)
            else "Require Explicit Selection"
        ),
        "options": options,
    }


def _legacy_option(
    legacy_id: str,
    option_name: str,
    price_adjustment: float,
    is_default: int,
    is_active: int,
    display_order: int,
) -> LegacyOption:
    return {
        "legacy_id": legacy_id,
        "option_name": option_name,
        "price_adjustment": price_adjustment,
        "is_default": is_default,
        "is_active": is_active,
        "display_order": display_order,
    }


class _InMemoryFBStore:
    def __init__(self, legacy_groups: list[LegacyGroup]) -> None:
        self.legacy_groups = legacy_groups
        self.fb_groups: dict[str, dict[str, object]] = {}
        self.fb_modifiers: dict[str, dict[str, object]] = {}
        self.commits = 0
        self.rollbacks = 0

    def get_all(self, doctype: str, **kwargs):
        if doctype != "KoPOS Modifier Group":
            return []
        ordered = sorted(
            self.legacy_groups,
            key=lambda row: (
                row["display_order"],
                str(row["group_name"]).lower(),
                str(row["legacy_id"]).lower(),
            ),
        )
        return [{"name": row["legacy_id"]} for row in ordered]

    def get_doc(self, doctype: str, name: str):
        if doctype == "KoPOS Modifier Group":
            for group in self.legacy_groups:
                if group["legacy_id"] == name:
                    return SimpleNamespace(
                        name=group["legacy_id"],
                        group_name=group["group_name"],
                        selection_type=group["selection_type"],
                        is_required=group["is_required"],
                        min_selections=group["min_selections"],
                        max_selections=group["max_selections"],
                        display_order=group["display_order"],
                        is_active=group["is_active"],
                        parent_option_id=group["parent_option_id"],
                        options=[
                            SimpleNamespace(name=option["legacy_id"], **option)
                            for option in group["options"]
                        ],
                    )
        storage = (
            self.fb_groups if doctype == "FB Modifier Group" else self.fb_modifiers
        )
        return _InMemoryDoc(self, doctype, dict(storage[name]))

    def new_doc(self, doctype: str):
        return _InMemoryDoc(self, doctype, {})

    def get_value(self, doctype: str, filters, fieldname: str):
        if fieldname != "name" or not isinstance(filters, dict):
            return None
        code_field = "group_code" if doctype == "FB Modifier Group" else "modifier_code"
        storage = (
            self.fb_groups if doctype == "FB Modifier Group" else self.fb_modifiers
        )
        expected_code = filters.get(code_field)
        for name, row in storage.items():
            if row.get(code_field) == expected_code:
                return name
        return None

    def exists(self, doctype: str, name: str):
        storage = (
            self.fb_groups if doctype == "FB Modifier Group" else self.fb_modifiers
        )
        return name in storage

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _InMemoryDoc:
    def __init__(
        self, store: _InMemoryFBStore, doctype: str, payload: dict[str, object]
    ) -> None:
        self._store = store
        self.doctype = doctype
        for key, value in payload.items():
            setattr(self, key, value)

    def insert(self, ignore_permissions: bool = False):
        self.name = getattr(
            self,
            "group_code" if self.doctype == "FB Modifier Group" else "modifier_code",
        )
        self._persist()

    def save(self, ignore_permissions: bool = False):
        if not getattr(self, "name", None):
            self.name = getattr(
                self,
                "group_code"
                if self.doctype == "FB Modifier Group"
                else "modifier_code",
            )
        self._persist()

    def _persist(self):
        target = (
            self._store.fb_groups
            if self.doctype == "FB Modifier Group"
            else self._store.fb_modifiers
        )
        target[self.name] = {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"_store", "doctype"}
        }


class TestFBModifierBackfillPlan(unittest.TestCase):
    def test_plan_preserves_parity_and_parent_modifier_links(self):
        from kopos_connector.api.modifier_migration import (
            _stable_fb_group_code,
            _stable_fb_modifier_code,
            build_fb_modifier_backfill_plan,
        )

        legacy_groups = [
            _legacy_group(
                legacy_id="grp-ice",
                group_name="Ice Level",
                selection_type="multiple",
                is_required=0,
                min_selections=0,
                max_selections=2,
                display_order=2,
                is_active=1,
                parent_option_id="opt-iced",
                options=[
                    _legacy_option("opt-less-ice", "Less Ice", 0.0, 0, 1, 2),
                    _legacy_option("opt-no-ice", "No Ice", 0.0, 0, 1, 1),
                ],
            ),
            _legacy_group(
                legacy_id="grp-temp",
                group_name="Temperature",
                selection_type="single",
                is_required=1,
                min_selections=0,
                max_selections=1,
                display_order=1,
                is_active=1,
                parent_option_id=None,
                options=[
                    _legacy_option("opt-hot", "Hot", 0.0, 0, 1, 2),
                    _legacy_option("opt-iced", "Iced", 0.0, 1, 1, 1),
                ],
            ),
        ]

        plan = build_fb_modifier_backfill_plan(legacy_groups)
        plan_groups = cast(list[dict[str, object]], plan["groups"])
        plan_modifiers = cast(list[dict[str, object]], plan["modifiers"])

        self.assertEqual(
            [group["group_code"] for group in plan_groups],
            [
                _stable_fb_group_code("grp-temp"),
                _stable_fb_group_code("grp-ice"),
            ],
        )
        self.assertEqual(
            [modifier["modifier_code"] for modifier in plan_modifiers],
            [
                _stable_fb_modifier_code("opt-iced"),
                _stable_fb_modifier_code("opt-hot"),
                _stable_fb_modifier_code("opt-no-ice"),
                _stable_fb_modifier_code("opt-less-ice"),
            ],
        )

        groups_by_code = {group["group_code"]: group for group in plan_groups}
        modifiers_by_code = {
            modifier["modifier_code"]: modifier for modifier in plan_modifiers
        }
        temperature_group = groups_by_code[_stable_fb_group_code("grp-temp")]
        ice_group = groups_by_code[_stable_fb_group_code("grp-ice")]

        self.assertEqual(temperature_group["selection_type"], "Single")
        self.assertEqual(temperature_group["is_required"], 1)
        self.assertEqual(temperature_group["display_order"], 1)
        self.assertEqual(
            temperature_group["default_resolution_policy"], "Auto Apply Default"
        )
        self.assertIsNone(temperature_group["parent_modifier"])

        self.assertEqual(ice_group["selection_type"], "Multiple")
        self.assertEqual(ice_group["min_selection"], 0)
        self.assertEqual(ice_group["max_selection"], 2)
        self.assertEqual(
            ice_group["parent_modifier"], _stable_fb_modifier_code("opt-iced")
        )

        iced_modifier = modifiers_by_code[_stable_fb_modifier_code("opt-iced")]
        no_ice_modifier = modifiers_by_code[_stable_fb_modifier_code("opt-no-ice")]
        self.assertEqual(
            iced_modifier["modifier_group"], temperature_group["group_code"]
        )
        self.assertEqual(iced_modifier["is_default"], 1)
        self.assertEqual(iced_modifier["kind"], "Instruction Only")
        self.assertEqual(no_ice_modifier["display_order"], 1)
        self.assertEqual(no_ice_modifier["active"], 1)

    def test_plan_rejects_unresolved_parent_option_links(self):
        from kopos_connector.api.modifier_migration import (
            build_fb_modifier_backfill_plan,
        )

        legacy_groups = [
            _legacy_group(
                legacy_id="grp-sauce",
                group_name="Sauce",
                selection_type="single",
                is_required=0,
                min_selections=0,
                max_selections=1,
                display_order=1,
                is_active=1,
                parent_option_id="opt-missing",
                options=[_legacy_option("opt-ketchup", "Ketchup", 0.0, 0, 1, 1)],
            )
        ]

        with self.assertRaises(Exception) as context:
            build_fb_modifier_backfill_plan(legacy_groups)

        self.assertIn(
            "parent_option_id references were not found", str(context.exception)
        )

    def test_plan_resolves_parent_links_when_parent_group_sorts_later(self):
        from kopos_connector.api.modifier_migration import (
            _stable_fb_group_code,
            _stable_fb_modifier_code,
            build_fb_modifier_backfill_plan,
        )

        legacy_groups = [
            _legacy_group(
                legacy_id="grp-ice",
                group_name="Ice Level",
                selection_type="multiple",
                is_required=0,
                min_selections=0,
                max_selections=2,
                display_order=1,
                is_active=1,
                parent_option_id="opt-iced",
                options=[_legacy_option("opt-no-ice", "No Ice", 0.0, 0, 1, 1)],
            ),
            _legacy_group(
                legacy_id="grp-temp",
                group_name="Temperature",
                selection_type="single",
                is_required=1,
                min_selections=0,
                max_selections=1,
                display_order=2,
                is_active=1,
                parent_option_id=None,
                options=[_legacy_option("opt-iced", "Iced", 0.0, 1, 1, 1)],
            ),
        ]

        plan = build_fb_modifier_backfill_plan(legacy_groups)
        plan_groups = cast(list[dict[str, object]], plan["groups"])
        groups_by_code = {group["group_code"]: group for group in plan_groups}

        self.assertEqual(
            groups_by_code[_stable_fb_group_code("grp-ice")]["parent_modifier"],
            _stable_fb_modifier_code("opt-iced"),
        )


class TestFBModifierBackfillExecution(unittest.TestCase):
    def test_backfill_is_idempotent_and_does_not_duplicate_records(self):
        from kopos_connector.api.modifier_migration import (
            _stable_fb_group_code,
            _stable_fb_modifier_code,
            backfill_kopos_modifiers_to_fb,
        )

        legacy_groups = [
            _legacy_group(
                legacy_id="grp-temp",
                group_name="Temperature",
                selection_type="single",
                is_required=1,
                min_selections=0,
                max_selections=1,
                display_order=1,
                is_active=1,
                parent_option_id=None,
                options=[
                    _legacy_option("opt-hot", "Hot", 0.0, 0, 1, 2),
                    _legacy_option("opt-iced", "Iced", 0.5, 1, 1, 1),
                ],
            ),
            _legacy_group(
                legacy_id="grp-ice",
                group_name="Ice Level",
                selection_type="multiple",
                is_required=0,
                min_selections=0,
                max_selections=2,
                display_order=2,
                is_active=1,
                parent_option_id="opt-iced",
                options=[_legacy_option("opt-no-ice", "No Ice", 0.0, 0, 1, 1)],
            ),
        ]
        store = _InMemoryFBStore(legacy_groups)

        with (
            patch(
                "kopos_connector.api.modifier_migration.frappe.get_all",
                side_effect=store.get_all,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.get_doc",
                side_effect=store.get_doc,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.new_doc",
                side_effect=store.new_doc,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.get_value",
                side_effect=store.get_value,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.exists",
                side_effect=store.exists,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.commit",
                side_effect=store.commit,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.db.rollback",
                side_effect=store.rollback,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.has_permission",
                return_value=True,
            ),
            patch(
                "kopos_connector.api.modifier_migration.frappe.log_error",
                MagicMock(),
            ),
        ):
            first_run = backfill_kopos_modifiers_to_fb()
            second_run = backfill_kopos_modifiers_to_fb()

        self.assertEqual(first_run["groups_created"], 2)
        self.assertEqual(first_run["modifiers_created"], 3)
        self.assertEqual(first_run["parent_links_updated"], 1)
        self.assertEqual(second_run["groups_created"], 0)
        self.assertEqual(second_run["groups_unchanged"], 2)
        self.assertEqual(second_run["modifiers_created"], 0)
        self.assertEqual(second_run["modifiers_unchanged"], 3)
        self.assertEqual(second_run["parent_links_unchanged"], 2)
        self.assertEqual(len(store.fb_groups), 2)
        self.assertEqual(len(store.fb_modifiers), 3)
        self.assertEqual(store.commits, 2)
        self.assertEqual(store.rollbacks, 0)

        temp_group = store.fb_groups[_stable_fb_group_code("grp-temp")]
        ice_group = store.fb_groups[_stable_fb_group_code("grp-ice")]
        iced_modifier = store.fb_modifiers[_stable_fb_modifier_code("opt-iced")]

        self.assertEqual(temp_group["default_resolution_policy"], "Auto Apply Default")
        self.assertEqual(
            ice_group["parent_modifier"], _stable_fb_modifier_code("opt-iced")
        )
        self.assertEqual(iced_modifier["price_adjustment"], 0.5)


if __name__ == "__main__":
    unittest.main()
