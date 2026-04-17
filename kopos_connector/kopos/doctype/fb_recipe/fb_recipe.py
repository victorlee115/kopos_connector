from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

frappe = import_module("frappe")
Document = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

cint = frappe_utils.cint
get_datetime = frappe_utils.get_datetime

from kopos_connector.kopos.services.recipe.resolver import resolve_components

if TYPE_CHECKING:
    from datetime import datetime


class FBRecipe(Document):
    def validate(self) -> None:
        self.validate_effective_dates()
        self.validate_version()
        self.validate_modifier_groups()

    def validate_effective_dates(self) -> None:
        if self.effective_from and self.effective_to:
            effective_from = get_datetime(self.effective_from)
            effective_to = get_datetime(self.effective_to)
            if effective_from > effective_to:
                frappe.throw("Effective To must be on or after Effective From")

    def validate_version(self) -> None:
        version_no = cint(self.version_no)
        if version_no <= 0:
            frappe.throw("Version No must be greater than 0")
        self.version_no = version_no

        duplicate_filters = {
            "sellable_item": self.sellable_item,
            "company": self.company,
            "version_no": version_no,
        }
        if not self.is_new():
            duplicate_filters["name"] = ["!=", self.name]

        duplicate_name = frappe.db.get_value("FB Recipe", duplicate_filters, "name")
        if duplicate_name:
            frappe.throw(
                f"Version {version_no} already exists for item {self.sellable_item} in company {self.company}"
            )

        if self.status != "Active":
            return

        for row in self.get("components") or []:
            if not row.item:
                frappe.throw("Each recipe component must define an Item")

        active_filters = {
            "sellable_item": self.sellable_item,
            "company": self.company,
            "status": "Active",
        }
        if not self.is_new():
            active_filters["name"] = ["!=", self.name]

        active_recipe_names = frappe.get_all(
            "FB Recipe",
            filters=active_filters,
            pluck="name",
            order_by="version_no desc",
        )

        current_start = (
            get_datetime(self.effective_from) if self.effective_from else None
        )
        current_end = get_datetime(self.effective_to) if self.effective_to else None

        for recipe_name in active_recipe_names:
            recipe = frappe.get_cached_doc("FB Recipe", recipe_name)
            other_start = (
                get_datetime(recipe.effective_from) if recipe.effective_from else None
            )
            other_end = (
                get_datetime(recipe.effective_to) if recipe.effective_to else None
            )
            if _date_ranges_overlap(current_start, current_end, other_start, other_end):
                frappe.throw(
                    f"Active recipe {recipe.name} overlaps with the effective date range for {self.sellable_item}"
                )

    def validate_modifier_groups(self) -> None:
        seen_groups: set[str] = set()
        for row in self.get("allowed_modifier_groups") or []:
            modifier_group = row.modifier_group
            if not modifier_group:
                frappe.throw("Allowed Modifier Group row must define a Modifier Group")
            if modifier_group in seen_groups:
                frappe.throw(f"Modifier Group {modifier_group} can only appear once")
            seen_groups.add(modifier_group)

            if row.default_modifier:
                modifier_values = frappe.db.get_value(
                    "FB Modifier",
                    row.default_modifier,
                    ["modifier_group", "active"],
                    as_dict=True,
                )
                if not modifier_values:
                    frappe.throw(
                        f"Default Modifier {row.default_modifier} was not found"
                    )
                if modifier_values.modifier_group != modifier_group:
                    frappe.throw(
                        f"Default Modifier {row.default_modifier} does not belong to Modifier Group {modifier_group}"
                    )
                if not cint(modifier_values.active):
                    frappe.throw(
                        f"Default Modifier {row.default_modifier} must be active"
                    )

            min_selection = cint(row.override_min_selection)
            max_selection = cint(row.override_max_selection)
            if min_selection and max_selection and min_selection > max_selection:
                frappe.throw(
                    f"Override Min Selection cannot be greater than Override Max Selection for group {modifier_group}"
                )

    def is_active_version(self, at_time: datetime | str | None = None) -> bool:
        if self.status != "Active":
            return False
        if at_time is None:
            current_time = get_datetime()
        else:
            current_time = get_datetime(at_time)
        effective_from = (
            get_datetime(self.effective_from) if self.effective_from else None
        )
        effective_to = get_datetime(self.effective_to) if self.effective_to else None
        if effective_from and current_time < effective_from:
            return False
        if effective_to and current_time > effective_to:
            return False
        return True

    def get_components_for_modifiers(
        self, selected_modifiers: list[object] | None = None
    ) -> list[dict[str, object]]:
        return resolve_components(self, selected_modifiers or [])


def _date_ranges_overlap(
    start_a: datetime | None,
    end_a: datetime | None,
    start_b: datetime | None,
    end_b: datetime | None,
) -> bool:
    normalized_start_a = start_a or get_datetime("1900-01-01 00:00:00")
    normalized_end_a = end_a or get_datetime("2999-12-31 23:59:59")
    normalized_start_b = start_b or get_datetime("1900-01-01 00:00:00")
    normalized_end_b = end_b or get_datetime("2999-12-31 23:59:59")
    return (
        normalized_start_a <= normalized_end_b
        and normalized_start_b <= normalized_end_a
    )
