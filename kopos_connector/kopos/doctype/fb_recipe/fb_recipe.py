from __future__ import annotations

from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import TYPE_CHECKING, Any

frappe = import_module("frappe")
Document = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

cint = frappe_utils.cint
get_datetime = frappe_utils.get_datetime

from kopos_connector.kopos.services.recipe.modifier_bounds import (
    ModifierBoundsError,
    resolve_effective_modifier_bounds,
)
from kopos_connector.kopos.services.recipe.resolver import resolve_components

if TYPE_CHECKING:
    from datetime import datetime


class FBRecipe(Document):
    def validate(self) -> None:
        self.validate_effective_dates()
        self.validate_version()
        self.validate_used_version_is_immutable()
        self.validate_modifier_groups()

    def validate_used_version_is_immutable(self) -> None:
        """Require a new version once a recipe may have reached a tablet."""

        is_new = getattr(self, "is_new", None)
        if (callable(is_new) and is_new()) or not getattr(self, "name", None):
            return
        get_before_save = getattr(self, "get_doc_before_save", None)
        if not callable(get_before_save):
            return
        previous = get_before_save()
        if not previous:
            return
        is_published = getattr(previous, "status", None) in {"Active", "Retired"}
        is_used = _recipe_is_used(self.name)
        if not is_published and not is_used:
            return
        if _recipe_definition_snapshot(previous) != _recipe_definition_snapshot(self):
            frappe.throw(
                f"FB Recipe {self.name} has already been published or used by a sale; create a new recipe version instead of changing its definition",
                frappe.ValidationError,
            )

    def before_rename(self, old: str, new: str, merge: bool = False) -> None:
        del new, merge
        if self.status in {"Active", "Retired"} or _recipe_is_used(old):
            frappe.throw(
                f"Published or used FB Recipe {old} cannot be renamed; create a new recipe version",
                frappe.ValidationError,
            )

    def on_trash(self) -> None:
        if self.status in {"Active", "Retired"} or _recipe_is_used(self.name):
            frappe.throw(
                f"Published or used FB Recipe {self.name} cannot be deleted",
                frappe.ValidationError,
            )

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

        _require_positive_decimal(self.yield_qty, "Yield Qty")
        _require_positive_decimal(self.default_serving_qty, "Default Serving Qty")
        if not self.yield_uom:
            frappe.throw("Yield UOM is required for an active recipe")
        if not self.default_serving_uom:
            frappe.throw("Default Serving UOM is required for an active recipe")

        components = list(self.get("components") or [])
        if not components:
            frappe.throw("An active FB Recipe must define at least one component")
        for row_index, row in enumerate(components, start=1):
            if not row.item:
                frappe.throw("Each recipe component must define an Item")
            _require_positive_decimal(
                row.qty,
                f"Recipe component {row_index} Qty",
            )
            if not row.uom:
                frappe.throw(f"Recipe component {row_index} UOM is required")

            stock_qty_value = (
                row.stock_qty
                if row.stock_qty is not None and row.stock_qty != ""
                else row.qty
            )
            _require_positive_decimal(
                stock_qty_value,
                f"Recipe component {row_index} Stock Qty",
            )
            if not cint(row.affects_stock):
                continue

            item_values = frappe.db.get_value(
                "Item",
                row.item,
                ["stock_uom", "is_stock_item"],
                as_dict=True,
            )
            if not item_values:
                frappe.throw(f"Recipe component Item {row.item} was not found")
            item_stock_uom = getattr(item_values, "stock_uom", None)
            if isinstance(item_values, dict):
                item_stock_uom = item_values.get("stock_uom")
                is_stock_item = cint(item_values.get("is_stock_item"))
            else:
                is_stock_item = cint(getattr(item_values, "is_stock_item", None))
            if not is_stock_item:
                frappe.throw(
                    f"Recipe component {row.item} affects stock and must be a stock Item"
                )
            resolved_stock_uom = row.stock_uom or row.uom
            if not resolved_stock_uom:
                frappe.throw(f"Recipe component {row_index} Stock UOM is required")
            if not item_stock_uom or resolved_stock_uom != item_stock_uom:
                frappe.throw(
                    f"Recipe component {row.item} Stock UOM must match Item stock UOM {item_stock_uom or '(missing)'}"
                )

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

            group_doc = frappe.get_cached_doc("FB Modifier Group", modifier_group)
            self.validate_modifier_group_selection_rules(row, group_doc)

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

    def validate_modifier_group_selection_rules(
        self,
        row: object,
        group_doc: object,
    ) -> None:
        """Keep every recipe on the modifier group's shared tablet rule."""

        modifier_group = getattr(row, "modifier_group", None) or "(missing)"
        recipe_name = (
            getattr(self, "name", None)
            or getattr(self, "recipe_code", None)
            or "(new recipe)"
        )
        group_values = {
            "selection_type": getattr(group_doc, "selection_type", None),
            "group_is_required": getattr(group_doc, "is_required", None),
            "group_min_selection": getattr(group_doc, "min_selection", None),
            "group_max_selection": getattr(group_doc, "max_selection", None),
        }
        try:
            shared_bounds = resolve_effective_modifier_bounds(**group_values)
        except ModifierBoundsError as error:
            frappe.throw(
                f"Modifier Group {modifier_group} has invalid selection rules: {error}",
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error

        try:
            recipe_bounds = resolve_effective_modifier_bounds(
                **group_values,
                recipe_required=getattr(row, "required", None),
                override_min_selection=getattr(row, "override_min_selection", None),
                override_max_selection=getattr(row, "override_max_selection", None),
            )
        except ModifierBoundsError as error:
            frappe.throw(
                "Recipe {0} has invalid selection rules for modifier group {1}: "
                "{2}. Use a separate modifier group if this recipe needs different "
                "rules.".format(recipe_name, modifier_group, error),
                frappe.ValidationError,
            )
            raise AssertionError("frappe.throw must raise") from error

        if recipe_bounds != shared_bounds:
            frappe.throw(
                "Recipe {0} changes the selection rules for modifier group {1}. "
                "Use a separate modifier group so every tablet uses the same rules.".format(
                    recipe_name,
                    modifier_group,
                ),
                frappe.ValidationError,
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


def _recipe_is_used(recipe_name: str) -> bool:
    return bool(
        frappe.db.exists("FB Order Line", {"recipe": recipe_name})
        or frappe.db.exists("FB Resolved Sale", {"recipe": recipe_name})
    )


def _require_positive_decimal(value: Any, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"{label} must be a finite number greater than 0")
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite() or parsed <= 0:
        frappe.throw(f"{label} must be a finite number greater than 0")
    return parsed


_COMPONENT_SNAPSHOT_FIELDS = (
    "item",
    "component_type",
    "qty",
    "uom",
    "stock_qty",
    "stock_uom",
    "is_optional",
    "is_substitutable",
    "substitution_key",
    "affects_stock",
    "affects_cogs",
    "loss_factor_pct",
    "sort_order",
)
_MODIFIER_GROUP_SNAPSHOT_FIELDS = (
    "modifier_group",
    "required",
    "override_min_selection",
    "override_max_selection",
    "default_modifier",
    "display_order",
    "always_prompt",
)


def _recipe_definition_snapshot(recipe: Any) -> tuple[Any, ...]:
    return (
        getattr(recipe, "sellable_item", None),
        getattr(recipe, "company", None),
        cint(getattr(recipe, "version_no", None)),
        getattr(recipe, "recipe_type", None),
        getattr(recipe, "yield_qty", None),
        getattr(recipe, "yield_uom", None),
        getattr(recipe, "default_serving_qty", None),
        getattr(recipe, "default_serving_uom", None),
        _child_rows_snapshot(
            getattr(recipe, "components", None) or [],
            _COMPONENT_SNAPSHOT_FIELDS,
        ),
        _child_rows_snapshot(
            getattr(recipe, "allowed_modifier_groups", None) or [],
            _MODIFIER_GROUP_SNAPSHOT_FIELDS,
        ),
    )


def _child_rows_snapshot(
    rows: list[Any], fieldnames: tuple[str, ...]
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(getattr(row, fieldname, None) for fieldname in fieldnames)
        for row in rows
    )
