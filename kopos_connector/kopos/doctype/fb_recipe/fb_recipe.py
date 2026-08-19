from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import TYPE_CHECKING, Any

frappe = import_module("frappe")
Document = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

cint = frappe_utils.cint
get_datetime = frappe_utils.get_datetime
cstr = frappe_utils.cstr

from kopos_connector.kopos.services.recipe.modifier_bounds import (
    ModifierBoundsError,
    resolve_effective_modifier_bounds,
)
from kopos_connector.kopos.services.recipe.resolver import resolve_components

if TYPE_CHECKING:
    from datetime import datetime


class FBRecipe(Document):
    def validate(self) -> None:
        self.populate_exact_decimal_fields()
        self.validate_effective_dates()
        # A historical version may contain rules that are no longer valid under
        # current authoring validation.  When someone tries to change that
        # version, report the real invariant first: published definitions are
        # immutable.  A status-only retirement still passes this check and can
        # safely take a bad legacy recipe out of the catalog.
        self.validate_used_version_is_immutable()
        retiring = self.is_status_only_retirement()
        if not retiring:
            self.validate_modifier_groups()
            if self.is_being_published():
                self.freeze_stock_component_conversions()
                self.freeze_modifier_effects()
                self.canonical_hash = _canonical_recipe_hash(self)
            elif self.status == "Active":
                self.validate_existing_canonical_hash()
        self.validate_version()

    def populate_exact_decimal_fields(self) -> None:
        """Keep exact authoring values beside Frappe's legacy Float fields.

        The hidden Data fields are the canonical values for new inventory
        recipes.  Legacy rows have no text snapshot, so their Float values are
        retained as the compatibility fallback.  When an editable draft's
        visible Float is changed, refresh its text value; when an import/API
        supplies the hidden text value, preserve that exact input.
        """

        self.yield_qty_decimal = _canonical_input(
            self, "yield_qty_decimal", "yield_qty", positive=True
        )
        self.default_serving_qty_decimal = _canonical_input(
            self, "default_serving_qty_decimal", "default_serving_qty", positive=True
        )
        for row in self.get("components") or []:
            row.qty_decimal = _canonical_input(row, "qty_decimal", "qty", positive=True)
            row.stock_qty_decimal = _canonical_input(
                row, "stock_qty_decimal", "stock_qty", positive=True
            )
            row.stock_conversion_factor_decimal = _canonical_input(
                row,
                "stock_conversion_factor_decimal",
                "stock_conversion_factor",
                positive=True,
            )
            row.loss_factor_pct_decimal = _canonical_input(
                row, "loss_factor_pct_decimal", "loss_factor_pct", positive=False
            )

    def is_being_published(self) -> bool:
        """True only for a new immutable recipe snapshot, never a later save."""

        if self.status != "Active":
            return False
        is_new = getattr(self, "is_new", None)
        if callable(is_new) and is_new():
            return True
        get_before_save = getattr(self, "get_doc_before_save", None)
        previous = get_before_save() if callable(get_before_save) else None
        return bool(previous and getattr(previous, "status", None) != "Active")

    def freeze_stock_component_conversions(self) -> None:
        """Store the exact entered-UOM conversion and normal loss at publish time."""

        for row_index, row in enumerate(self.get("components") or [], start=1):
            if not cint(getattr(row, "affects_stock", 0)):
                continue
            entered_qty = _require_positive_decimal(
                _decimal_field(row, "qty_decimal", "qty"),
                f"Recipe component {row_index} Qty",
            )
            entered_uom = cstr(getattr(row, "uom", None)).strip()
            item_code = cstr(getattr(row, "item", None)).strip()
            stock_uom, conversion = _stock_conversion_for_item(
                item_code=item_code,
                entered_uom=entered_uom,
                label=f"Recipe component {row_index}",
            )
            loss_percent = _non_negative_decimal(
                _decimal_field(row, "loss_factor_pct_decimal", "loss_factor_pct"),
                f"Recipe component {row_index} Loss Factor %",
            )
            if loss_percent > Decimal("100"):
                frappe.throw(
                    f"Recipe component {row_index} Loss Factor % cannot exceed 100",
                    frappe.ValidationError,
                )
            row.stock_uom = stock_uom
            row.stock_conversion_factor = str(conversion)
            row.stock_conversion_factor_decimal = _plain_decimal(conversion)
            row.qty_decimal = _plain_decimal(entered_qty)
            row.loss_factor_pct_decimal = _plain_decimal(loss_percent)
            exact_stock_qty = entered_qty * conversion * (
                Decimal("1") + loss_percent / Decimal("100")
            )
            row.stock_qty = str(
                exact_stock_qty.normalize()
            )
            row.stock_qty_decimal = _plain_decimal(exact_stock_qty)

    def freeze_modifier_effects(self) -> None:
        """Copy all selectable operational effects into this recipe version once."""

        group_ids = sorted(
            {
                cstr(getattr(row, "modifier_group", None)).strip()
                for row in self.get("allowed_modifier_groups") or []
                if cstr(getattr(row, "modifier_group", None)).strip()
            }
        )
        if not group_ids:
            self.set("recipe_modifier_effects", [])
            return

        modifier_rows = frappe.get_all(
            "FB Modifier",
            filters={"modifier_group": ["in", group_ids], "active": 1},
            fields=[
                "name",
                "modifier_group",
                "kind",
                "target_substitution_key",
                "target_item",
                "new_item",
                "qty_delta",
                "qty_delta_decimal",
                "qty_uom",
                "scale_percent",
                "scale_percent_decimal",
                "affects_stock",
                "affects_recipe",
            ],
            order_by="modifier_group asc, display_order asc, name asc",
        )
        frozen_rows = [
            _frozen_modifier_effect_row(self, modifier)
            for modifier in modifier_rows
        ]
        self.set("recipe_modifier_effects", frozen_rows)

    def validate_existing_canonical_hash(self) -> None:
        """Legacy pre-cutover recipes may remain readable but cannot enter inventory."""

        stored_hash = cstr(getattr(self, "canonical_hash", None)).strip()
        if not stored_hash:
            return
        expected_hash = _canonical_recipe_hash(self)
        if stored_hash != expected_hash:
            frappe.throw(
                "FB Recipe canonical hash does not match its frozen definition; create a new recipe version",
                frappe.ValidationError,
            )

    def is_status_only_retirement(self) -> bool:
        """Allow an invalid legacy recipe to leave the active catalog safely."""

        if getattr(self, "status", None) != "Retired":
            return False
        get_before_save = getattr(self, "get_doc_before_save", None)
        if not callable(get_before_save):
            return False
        previous = get_before_save()
        return bool(
            previous
            and getattr(previous, "status", None) == "Active"
            and _recipe_retirement_snapshot(previous)
            == _recipe_retirement_snapshot(self)
        )

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
        if self.effective_from or self.effective_to:
            configured_zone = cstr(frappe.db.get_single_value("System Settings", "time_zone")).strip()
            if configured_zone != "Asia/Kuala_Lumpur":
                frappe.throw("Recipe effective times require System Settings time zone Asia/Kuala_Lumpur")
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

        _require_positive_decimal(
            _decimal_field(self, "yield_qty_decimal", "yield_qty"),
            "Yield Qty",
        )
        _require_positive_decimal(
            _decimal_field(self, "default_serving_qty_decimal", "default_serving_qty"),
            "Default Serving Qty",
        )
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
                _decimal_field(row, "qty_decimal", "qty"),
                f"Recipe component {row_index} Qty",
            )
            if not row.uom:
                frappe.throw(f"Recipe component {row_index} UOM is required")

            stock_qty_value = (
                _decimal_field(row, "stock_qty_decimal", "stock_qty")
                or _decimal_field(row, "qty_decimal", "qty")
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


def _non_negative_decimal(value: Any, label: str) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as error:
        frappe.throw(f"{label} must be a finite number greater than or equal to 0")
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite() or parsed < 0:
        frappe.throw(f"{label} must be a finite number greater than or equal to 0")
    return parsed


def _stock_conversion_for_item(
    *, item_code: str, entered_uom: str, label: str
) -> tuple[str, Decimal]:
    if not item_code:
        frappe.throw(f"{label} Item is required", frappe.ValidationError)
    if not entered_uom:
        frappe.throw(f"{label} UOM is required", frappe.ValidationError)
    item_values = frappe.db.get_value(
        "Item",
        item_code,
        ["stock_uom", "is_stock_item"],
        as_dict=True,
    )
    if not item_values:
        frappe.throw(f"{label} Item {item_code} was not found", frappe.ValidationError)
    stock_uom = cstr(getattr(item_values, "stock_uom", None)).strip()
    if isinstance(item_values, dict):
        stock_uom = cstr(item_values.get("stock_uom")).strip()
        is_stock_item = cint(item_values.get("is_stock_item"))
    else:
        is_stock_item = cint(getattr(item_values, "is_stock_item", None))
    if not is_stock_item or not stock_uom:
        frappe.throw(
            f"{label} Item {item_code} must be a stock Item with a stock UOM",
            frappe.ValidationError,
        )
    if entered_uom == stock_uom:
        return stock_uom, Decimal("1")
    raw_conversion = frappe.db.get_value(
        "UOM Conversion Detail",
        {"parent": item_code, "uom": entered_uom},
        "conversion_factor",
    )
    conversion = _require_positive_decimal(
        raw_conversion,
        f"{label} UOM conversion from {entered_uom} to {stock_uom}",
    )
    return stock_uom, conversion


def _frozen_modifier_effect_row(recipe: FBRecipe, modifier: Any) -> dict[str, Any]:
    effect = {
        "modifier_group": cstr(getattr(modifier, "modifier_group", None)).strip(),
        "modifier": cstr(getattr(modifier, "name", None)).strip(),
        "kind": cstr(getattr(modifier, "kind", None)).strip(),
        "target_substitution_key": cstr(
            getattr(modifier, "target_substitution_key", None)
        ).strip()
        or None,
        "target_item": cstr(getattr(modifier, "target_item", None)).strip() or None,
        "new_item": cstr(getattr(modifier, "new_item", None)).strip() or None,
        "qty_delta": _decimal_field(modifier, "qty_delta_decimal", "qty_delta"),
        "qty_delta_decimal": _canonical_input(
            modifier, "qty_delta_decimal", "qty_delta", positive=True
        ),
        "qty_uom": cstr(getattr(modifier, "qty_uom", None)).strip() or None,
        "stock_qty_delta": None,
        "stock_qty_delta_decimal": None,
        "stock_uom": None,
        "stock_conversion_factor": None,
        "stock_conversion_factor_decimal": None,
        "scale_percent": _decimal_field(
            modifier, "scale_percent_decimal", "scale_percent"
        ),
        "scale_percent_decimal": _canonical_input(
            modifier, "scale_percent_decimal", "scale_percent", positive=False
        ),
        "affects_stock": cint(getattr(modifier, "affects_stock", 0)),
        "affects_recipe": cint(getattr(modifier, "affects_recipe", 0)),
    }
    modifier_name = effect["modifier"] or "(unnamed)"
    kind = effect["kind"]
    if kind not in {"Instruction Only", "Add", "Replace", "Remove", "Scale"}:
        frappe.throw(
            f"Modifier {modifier_name} has an unsupported effect kind {kind or '(missing)'}",
            frappe.ValidationError,
        )
    if not effect["affects_recipe"]:
        return effect
    if kind == "Add":
        target_item = effect["new_item"] or effect["target_item"]
        if not target_item:
            frappe.throw(
                f"Modifier {modifier_name} requires a new or target Item for Add",
                frappe.ValidationError,
            )
        entered_qty = _require_positive_decimal(
            effect["qty_delta"], f"Modifier {modifier_name} Quantity Delta"
        )
        entered_uom = cstr(effect["qty_uom"]).strip()
        if not entered_uom:
            frappe.throw(
                f"Modifier {modifier_name} requires a Quantity UOM for Add",
                frappe.ValidationError,
            )
        if effect["affects_stock"]:
            stock_uom, conversion = _stock_conversion_for_item(
                item_code=cstr(target_item),
                entered_uom=entered_uom,
                label=f"Modifier {modifier_name}",
            )
            effect["stock_uom"] = stock_uom
            effect["stock_conversion_factor"] = str(conversion)
            effect["stock_conversion_factor_decimal"] = _plain_decimal(conversion)
            exact_delta = entered_qty * conversion
            effect["stock_qty_delta"] = str(exact_delta.normalize())
            effect["stock_qty_delta_decimal"] = _plain_decimal(exact_delta)
            effect["qty_delta"] = entered_qty
            effect["qty_delta_decimal"] = _plain_decimal(entered_qty)
        return effect
    if kind == "Replace":
        if not effect["new_item"]:
            frappe.throw(
                f"Modifier {modifier_name} requires a New Item for Replace",
                frappe.ValidationError,
            )
        target_rows = _replacement_target_rows(recipe, effect)
        if not target_rows:
            frappe.throw(
                f"Modifier {modifier_name} does not target a component in recipe {recipe.name}",
                frappe.ValidationError,
            )
        if effect["affects_stock"]:
            new_item_stock_uom, _ = _stock_conversion_for_item(
                item_code=cstr(effect["new_item"]),
                entered_uom=_item_stock_uom(cstr(effect["new_item"]), f"Modifier {modifier_name}"),
                label=f"Modifier {modifier_name}",
            )
            for row in target_rows:
                if not cint(getattr(row, "affects_stock", 0)):
                    continue
                target_stock_uom = cstr(getattr(row, "stock_uom", None)).strip()
                if target_stock_uom != new_item_stock_uom:
                    frappe.throw(
                        "Modifier {0} replaces a stock component with a different stock UOM. "
                        "Use separate Remove and Add modifiers with explicit quantities.".format(
                            modifier_name
                        ),
                        frappe.ValidationError,
                    )
            effect["stock_uom"] = new_item_stock_uom
            if effect["qty_delta"] not in (None, "", 0, "0"):
                entered_qty = _require_positive_decimal(
                    effect["qty_delta"], f"Modifier {modifier_name} Quantity Delta"
                )
                entered_uom = cstr(effect["qty_uom"]).strip()
                if not entered_uom:
                    frappe.throw(
                        f"Modifier {modifier_name} requires a Quantity UOM for Replace delta",
                        frappe.ValidationError,
                    )
                _, conversion = _stock_conversion_for_item(
                    item_code=cstr(effect["new_item"]),
                    entered_uom=entered_uom,
                    label=f"Modifier {modifier_name}",
                )
                effect["stock_conversion_factor"] = str(conversion)
                effect["stock_conversion_factor_decimal"] = _plain_decimal(conversion)
                exact_delta = entered_qty * conversion
                effect["stock_qty_delta"] = str(exact_delta.normalize())
                effect["stock_qty_delta_decimal"] = _plain_decimal(exact_delta)
                effect["qty_delta"] = entered_qty
                effect["qty_delta_decimal"] = _plain_decimal(entered_qty)
        return effect
    if kind == "Scale":
        if _require_positive_decimal(
            effect["scale_percent"], f"Modifier {modifier_name} Scale Percent"
        ) <= 0:
            frappe.throw(
                f"Modifier {modifier_name} Scale Percent must be greater than 0",
                frappe.ValidationError,
            )
    return effect


def _item_stock_uom(item_code: str, label: str) -> str:
    values = frappe.db.get_value("Item", item_code, ["stock_uom", "is_stock_item"], as_dict=True)
    if not values:
        frappe.throw(f"{label} Item {item_code} was not found", frappe.ValidationError)
    stock_uom = cstr(
        values.get("stock_uom") if isinstance(values, dict) else getattr(values, "stock_uom", None)
    ).strip()
    is_stock_item = cint(
        values.get("is_stock_item") if isinstance(values, dict) else getattr(values, "is_stock_item", None)
    )
    if not stock_uom or not is_stock_item:
        frappe.throw(
            f"{label} Item {item_code} must be a stock Item with a stock UOM",
            frappe.ValidationError,
        )
    return stock_uom


def _replacement_target_rows(recipe: FBRecipe, effect: dict[str, Any]) -> list[Any]:
    substitution_key = cstr(effect.get("target_substitution_key")).strip()
    target_item = cstr(effect.get("target_item")).strip()
    return [
        row
        for row in recipe.get("components") or []
        if (substitution_key and cstr(getattr(row, "substitution_key", None)).strip() == substitution_key)
        or (not substitution_key and target_item and cstr(getattr(row, "item", None)).strip() == target_item)
    ]


def _canonical_recipe_hash(recipe: FBRecipe) -> str:
    payload = {
        "recipe": cstr(getattr(recipe, "name", None) or getattr(recipe, "recipe_code", None)).strip(),
        "version": cint(getattr(recipe, "version_no", None)),
        "sellable_item": cstr(getattr(recipe, "sellable_item", None)).strip(),
        "company": cstr(getattr(recipe, "company", None)).strip(),
        "yield_qty": _canonical_decimal(
            _decimal_field(recipe, "yield_qty_decimal", "yield_qty")
        ),
        "yield_uom": cstr(getattr(recipe, "yield_uom", None)).strip(),
        "default_serving_qty": _canonical_decimal(
            _decimal_field(recipe, "default_serving_qty_decimal", "default_serving_qty")
        ),
        "default_serving_uom": cstr(
            getattr(recipe, "default_serving_uom", None)
        ).strip(),
        "components": [
            {
                fieldname: _canonical_recipe_field(
                    fieldname, _decimal_field_for_snapshot(row, fieldname)
                )
                for fieldname in _COMPONENT_SNAPSHOT_FIELDS
            }
            for row in recipe.get("components") or []
        ],
        "modifier_groups": [
            {
                fieldname: _canonical_recipe_field(
                    fieldname, _decimal_field_for_snapshot(row, fieldname)
                )
                for fieldname in _MODIFIER_GROUP_SNAPSHOT_FIELDS
            }
            for row in recipe.get("allowed_modifier_groups") or []
        ],
        "modifier_effects": [
            {
                fieldname: _canonical_recipe_field(
                    fieldname, _decimal_field_for_snapshot(row, fieldname)
                )
                for fieldname in _MODIFIER_EFFECT_SNAPSHOT_FIELDS
            }
            for row in recipe.get("recipe_modifier_effects") or []
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


_NUMERIC_RECIPE_FIELDS = {
    "qty",
    "stock_qty",
    "stock_conversion_factor",
    "is_optional",
    "is_substitutable",
    "affects_stock",
    "affects_cogs",
    "loss_factor_pct",
    "sort_order",
    "required",
    "override_min_selection",
    "override_max_selection",
    "display_order",
    "always_prompt",
    "qty_delta",
    "stock_qty_delta",
    "scale_percent",
    "affects_recipe",
}


def _canonical_recipe_field(fieldname: str, value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return int(value)
    if fieldname in _NUMERIC_RECIPE_FIELDS:
        try:
            return _canonical_finite_decimal(value)
        except (InvalidOperation, TypeError, ValueError):
            return str(value)
    return cstr(value).strip()


def _canonical_decimal(value: Any) -> str:
    return format(_require_positive_decimal(value, "Recipe decimal").normalize(), "f")


def _canonical_finite_decimal(value: Any) -> str:
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise InvalidOperation
    normalized = parsed.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


_DECIMAL_FIELD_ALIASES = {
    "yield_qty": "yield_qty_decimal",
    "default_serving_qty": "default_serving_qty_decimal",
    "qty": "qty_decimal",
    "stock_qty": "stock_qty_decimal",
    "stock_conversion_factor": "stock_conversion_factor_decimal",
    "loss_factor_pct": "loss_factor_pct_decimal",
    "qty_delta": "qty_delta_decimal",
    "stock_qty_delta": "stock_qty_delta_decimal",
    "scale_percent": "scale_percent_decimal",
}


def _decimal_field(value: Any, canonical_field: str, legacy_field: str) -> Any:
    """Read canonical decimal text and retain a legacy Float fallback."""

    exact = getattr(value, canonical_field, None)
    return exact if exact not in (None, "") else getattr(value, legacy_field, None)


def _canonical_input(
    value: Any, canonical_field: str, legacy_field: str, *, positive: bool
) -> str | None:
    """Return normalized text without inventing values for blank draft fields."""

    raw = _decimal_field(value, canonical_field, legacy_field)
    if raw in (None, ""):
        return None
    try:
        parsed = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        return cstr(raw).strip() or None
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        return cstr(raw).strip() or None
    return _plain_decimal(parsed)


def _decimal_field_for_snapshot(value: Any, fieldname: str) -> Any:
    canonical = _DECIMAL_FIELD_ALIASES.get(fieldname)
    if canonical:
        return _decimal_field(value, canonical, fieldname)
    return getattr(value, fieldname, None)


def _plain_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f") if normalized != 0 else "0"


_COMPONENT_SNAPSHOT_FIELDS = (
    "item",
    "component_type",
    "qty",
    "uom",
    "stock_qty",
    "stock_uom",
    "stock_conversion_factor",
    "is_optional",
    "is_substitutable",
    "substitution_key",
    "affects_stock",
    "affects_cogs",
    "loss_factor_pct",
    "sort_order",
    "remarks",
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
_MODIFIER_EFFECT_SNAPSHOT_FIELDS = (
    "modifier_group",
    "modifier",
    "kind",
    "target_substitution_key",
    "target_item",
    "new_item",
    "qty_delta",
    "qty_uom",
    "stock_qty_delta",
    "stock_uom",
    "stock_conversion_factor",
    "scale_percent",
    "affects_stock",
    "affects_recipe",
)


def _recipe_definition_snapshot(recipe: Any) -> tuple[Any, ...]:
    return (
        getattr(recipe, "sellable_item", None),
        getattr(recipe, "company", None),
        cint(getattr(recipe, "version_no", None)),
        getattr(recipe, "recipe_type", None),
        _canonical_recipe_field(
            "yield_qty", _decimal_field(recipe, "yield_qty_decimal", "yield_qty")
        ),
        getattr(recipe, "yield_uom", None),
        _canonical_recipe_field(
            "default_serving_qty",
            _decimal_field(recipe, "default_serving_qty_decimal", "default_serving_qty"),
        ),
        getattr(recipe, "default_serving_uom", None),
        _child_rows_snapshot(
            getattr(recipe, "components", None) or [],
            _COMPONENT_SNAPSHOT_FIELDS,
        ),
        _child_rows_snapshot(
            getattr(recipe, "allowed_modifier_groups", None) or [],
            _MODIFIER_GROUP_SNAPSHOT_FIELDS,
        ),
        _child_rows_snapshot(
            getattr(recipe, "recipe_modifier_effects", None) or [],
            _MODIFIER_EFFECT_SNAPSHOT_FIELDS,
        ),
        getattr(recipe, "canonical_hash", None),
    )


def _recipe_retirement_snapshot(recipe: Any) -> tuple[Any, ...]:
    """All authored fields that must stay unchanged during Active -> Retired."""

    return (
        getattr(recipe, "name", None),
        getattr(recipe, "recipe_code", None),
        getattr(recipe, "recipe_name", None),
        getattr(recipe, "station", None),
        getattr(recipe, "effective_from", None),
        getattr(recipe, "effective_to", None),
        getattr(recipe, "notes", None),
        _recipe_definition_snapshot(recipe),
    )


def _child_rows_snapshot(
    rows: list[Any], fieldnames: tuple[str, ...]
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        tuple(
            _canonical_recipe_field(
                fieldname, _decimal_field_for_snapshot(row, fieldname)
            )
            for fieldname in fieldnames
        )
        for row in rows
    )
