from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from importlib import import_module
from typing import Any

frappe = import_module("frappe")
BaseDocument = import_module("frappe.model.document").Document
frappe_utils = import_module("frappe.utils")

flt = frappe_utils.flt
now_datetime = frappe_utils.now_datetime
DocumentLike = Any

from kopos_connector.kopos.doctype.fb_modifier_group.fb_modifier_group import (
    filter_visible_allowed_modifier_groups,
)
from kopos_connector.kopos.services.accounting.sales_invoice_service import (
    create_sales_invoice,
)
from kopos_connector.kopos.services.inventory.stock_issue_service import (
    create_ingredient_stock_entry,
)
from kopos_connector.kopos.services.projection.log_service import (
    create_projection_log,
    update_projection_state,
)


def cstr(value: Any) -> str:
    return str(frappe.utils.cstr(value))


class FBOrder(BaseDocument):
    def get_selected_modifier_rows(self, line) -> list[Any]:
        persisted_rows = list(line.get("selected_modifiers") or [])
        if persisted_rows:
            return persisted_rows

        transient_rows = getattr(line, "_selected_modifiers_payload", None)
        if not transient_rows:
            return []

        return list(transient_rows)

    def validate(self):
        self.validate_required_fields()
        self.calculate_totals()
        self.validate_order_totals()
        self.validate_idempotency_key_uniqueness()

    def before_submit(self):
        line_resolutions = self.build_line_resolutions()
        self.validate_stock_availability(line_resolutions)
        self.create_resolved_sales(line_resolutions)

    def on_submit(self):
        resolved_sales = self.get_resolved_sales()
        invoice_log = self.create_projection_entry("Sales Invoice")
        stock_log = self.create_projection_entry("Stock Issue")

        self.sales_invoice = create_sales_invoice(self)
        if self.sales_invoice:
            self.invoice_status = "Posted"
            update_projection_state(
                invoice_log,
                "Succeeded",
                "Sales Invoice",
                self.sales_invoice,
                None,
            )
        else:
            self.invoice_status = "Failed"
            update_projection_state(
                invoice_log,
                "Failed",
                "Sales Invoice",
                None,
                "Sales Invoice projection failed",
            )

        self.ingredient_stock_entry = create_ingredient_stock_entry(
            self, resolved_sales
        )
        if self.ingredient_stock_entry:
            self.stock_status = "Posted"
            update_projection_state(
                stock_log,
                "Succeeded",
                "Stock Entry",
                self.ingredient_stock_entry,
                None,
            )
        else:
            self.stock_status = "Failed"
            update_projection_state(
                stock_log,
                "Failed",
                "Stock Entry",
                None,
                "Stock issue projection failed",
            )

        self.status = "Submitted"
        self.db_set("status", self.status, update_modified=False)
        self.db_set("invoice_status", self.invoice_status, update_modified=False)
        self.db_set("stock_status", self.stock_status, update_modified=False)
        if self.sales_invoice:
            self.db_set("sales_invoice", self.sales_invoice, update_modified=False)
        if self.ingredient_stock_entry:
            self.db_set(
                "ingredient_stock_entry",
                self.ingredient_stock_entry,
                update_modified=False,
            )
        self.update_shift_expected_cash()

    def create_projection_entry(self, projection_type: str) -> str:
        return (
            create_projection_log(
                source_doctype="FB Order",
                source_name=self.name,
                projection_type=projection_type,
                idempotency_key=f"{self.external_idempotency_key}:{projection_type}",
                payload_hash=self.build_projection_hash(projection_type),
            )
            or ""
        )

    def build_projection_hash(self, projection_type: str) -> str:
        payload = {
            "order": self.name,
            "projection_type": projection_type,
            "status": self.status,
            "invoice_status": self.invoice_status,
            "stock_status": self.stock_status,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def get_resolved_sales(self) -> list[DocumentLike]:
        resolved_sales = []
        for line in self.items:
            if getattr(line, "resolved_sale", None):
                resolved_sales.append(
                    frappe.get_doc("FB Resolved Sale", line.resolved_sale)
                )
        return resolved_sales

    def update_shift_expected_cash(self):
        if not self.shift:
            return
        shift_doc = frappe.get_doc("FB Shift", self.shift)
        orders = frappe.get_all(
            "FB Order",
            filters={"shift": self.shift, "status": "Submitted"},
            fields=["sales_invoice"],
        )
        total_cash = 0.0
        for order in orders:
            if not order.sales_invoice:
                continue
            sales_invoice = frappe.get_doc("Sales Invoice", order.sales_invoice)
            for payment in sales_invoice.get("payments") or []:
                if cstr(getattr(payment, "mode_of_payment", None)) == "Cash":
                    total_cash += flt(getattr(payment, "amount", 0))
        shift_doc.db_set(
            "expected_cash",
            flt(getattr(shift_doc, "opening_float", 0)) + total_cash,
            update_modified=False,
        )
        if getattr(shift_doc, "counted_cash", None) is not None:
            shift_doc.db_set(
                "cash_variance",
                flt(getattr(shift_doc, "counted_cash", 0))
                - flt(getattr(shift_doc, "expected_cash", 0)),
                update_modified=False,
            )

    def calculate_totals(self):
        net_total = 0.0
        for line in self.get("items") or []:
            unit_price = flt(line.unit_price)
            modifier_total = flt(line.modifier_total)
            discount_amount = flt(line.discount_amount)
            qty = flt(line.qty)
            computed_line_total = (
                (unit_price + modifier_total) * qty
            ) - discount_amount
            line.line_total = computed_line_total
            net_total += computed_line_total

        self.net_total = net_total
        self.tax_total = flt(self.tax_total)
        self.rounding_adjustment = flt(getattr(self, "rounding_adjustment", 0) or 0)
        self.grand_total = (
            flt(self.net_total) + flt(self.tax_total) + flt(self.rounding_adjustment)
        )

    def validate_required_fields(self):
        required_order_fields = {
            "order_id": self.order_id,
            "external_idempotency_key": self.external_idempotency_key,
            "source": self.source,
            "device_id": self.device_id,
            "shift": self.shift,
            "staff_id": self.staff_id,
            "booth_warehouse": self.booth_warehouse,
            "company": self.company,
            "currency": self.currency,
        }
        missing_order_fields = [
            fieldname for fieldname, value in required_order_fields.items() if not value
        ]
        if missing_order_fields:
            frappe.throw(
                "FB Order is missing required fields: {0}".format(
                    ", ".join(missing_order_fields)
                ),
                frappe.ValidationError,
            )

        if not self.get("items"):
            frappe.throw(
                "FB Order must contain at least one item", frappe.ValidationError
            )

        for line_index, line in enumerate(self.items, start=1):
            required_line_fields = {
                "line_id": line.line_id,
                "item": line.item,
                "qty": line.qty,
                "uom": line.uom,
            }
            missing_line_fields = [
                fieldname
                for fieldname, value in required_line_fields.items()
                if not value
            ]
            if missing_line_fields:
                frappe.throw(
                    "Order line {0} is missing required fields: {1}".format(
                        self.describe_line(line_index, line),
                        ", ".join(missing_line_fields),
                    ),
                    frappe.ValidationError,
                )

            if flt(line.qty) <= 0:
                frappe.throw(
                    "Order line {0} must have qty greater than 0".format(
                        self.describe_line(line_index, line)
                    ),
                    frappe.ValidationError,
                )

        for payment_index, payment in enumerate(self.get("payments") or [], start=1):
            if not payment.payment_method:
                frappe.throw(
                    "Payment row {0} is missing payment_method".format(payment_index),
                    frappe.ValidationError,
                )
            if flt(payment.amount) <= 0:
                frappe.throw(
                    "Payment row {0} must have amount greater than 0".format(
                        payment_index
                    ),
                    frappe.ValidationError,
                )

    def validate_order_totals(self):
        expected_net_total = sum(flt(line.line_total) for line in self.items)
        if abs(flt(self.net_total) - expected_net_total) > 0.0001:
            frappe.throw(
                "FB Order net_total {0} does not match summed line totals {1}".format(
                    flt(self.net_total), expected_net_total
                ),
                frappe.ValidationError,
            )

        expected_grand_total = (
            expected_net_total
            + flt(self.tax_total)
            + flt(getattr(self, "rounding_adjustment", 0) or 0)
        )
        if abs(flt(self.grand_total) - expected_grand_total) > 0.0001:
            frappe.throw(
                "FB Order grand_total {0} does not match net_total plus tax_total plus rounding_adjustment {1}".format(
                    flt(self.grand_total), expected_grand_total
                ),
                frappe.ValidationError,
            )

        payment_total = sum(
            flt(payment.amount) for payment in self.get("payments") or []
        )
        if payment_total and abs(payment_total - flt(self.grand_total)) > 0.0001:
            frappe.throw(
                "FB Order payment total {0} does not match grand_total {1}".format(
                    payment_total, flt(self.grand_total)
                ),
                frappe.ValidationError,
            )

    def validate_idempotency_key_uniqueness(self):
        duplicate_name = frappe.db.get_value(
            "FB Order",
            {
                "external_idempotency_key": self.external_idempotency_key,
                "name": ["!=", self.name or ""],
            },
            "name",
        )
        if duplicate_name:
            frappe.throw(
                "Idempotency key {0} is already used by FB Order {1}".format(
                    self.external_idempotency_key, duplicate_name
                ),
                frappe.ValidationError,
            )

    def build_line_resolutions(self) -> list[dict[str, Any]]:
        line_resolutions = []
        for line_index, line in enumerate(self.items, start=1):
            recipe_doc = self.resolve_recipe_for_line(line_index, line)
            selected_modifiers = self.validate_modifier_selections(
                line_index=line_index,
                line=line,
                recipe_doc=recipe_doc,
            )
            resolved_components = self.resolve_components_for_line(
                line_index=line_index,
                line=line,
                recipe_doc=recipe_doc,
                selected_modifiers=selected_modifiers,
            )
            line_resolutions.append(
                {
                    "line": line,
                    "line_index": line_index,
                    "recipe_doc": recipe_doc,
                    "selected_modifiers": selected_modifiers,
                    "resolved_components": resolved_components,
                }
            )
        return line_resolutions

    def resolve_recipe_for_line(self, line_index: int, line) -> DocumentLike:
        if line.recipe:
            recipe_doc = frappe.get_cached_doc("FB Recipe", line.recipe)
        else:
            recipe_doc = self.find_default_recipe_for_item(line.item)
            line.recipe = recipe_doc.name

        if recipe_doc.status != "Active":
            frappe.throw(
                "Order line {0} references inactive recipe {1}".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )

        if recipe_doc.sellable_item != line.item:
            frappe.throw(
                "Order line {0} recipe {1} does not match sellable item {2}".format(
                    self.describe_line(line_index, line), recipe_doc.name, line.item
                ),
                frappe.ValidationError,
            )

        if recipe_doc.company and recipe_doc.company != self.company:
            frappe.throw(
                "Order line {0} recipe {1} belongs to company {2}, expected {3}".format(
                    self.describe_line(line_index, line),
                    recipe_doc.name,
                    recipe_doc.company,
                    self.company,
                ),
                frappe.ValidationError,
            )

        if not self.recipe_is_effective(recipe_doc):
            frappe.throw(
                "Order line {0} recipe {1} is not effective at submit time".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )

        if not recipe_doc.components:
            frappe.throw(
                "Order line {0} recipe {1} has no components to resolve".format(
                    self.describe_line(line_index, line), recipe_doc.name
                ),
                frappe.ValidationError,
            )

        item_is_stock = frappe.db.get_value("Item", line.item, "is_stock_item")
        if item_is_stock and int(line.is_recipe_managed or 1):
            frappe.throw(
                "Order line {0} item {1} is recipe-managed and must be configured as a non-stock item".format(
                    self.describe_line(line_index, line), line.item
                ),
                frappe.ValidationError,
            )

        line.is_recipe_managed = 1
        line.recipe_version = recipe_doc.version_no
        line.item_name_snapshot = line.item_name_snapshot or recipe_doc.recipe_name
        return recipe_doc

    def find_default_recipe_for_item(self, item_code: str) -> DocumentLike:
        candidate_names = frappe.get_all(
            "FB Recipe",
            filters={
                "sellable_item": item_code,
                "status": "Active",
                "company": self.company,
            },
            pluck="name",
        )
        effective_candidates = []
        for candidate_name in candidate_names:
            recipe_doc = frappe.get_cached_doc("FB Recipe", candidate_name)
            if self.recipe_is_effective(recipe_doc):
                effective_candidates.append(recipe_doc)

        if not effective_candidates:
            frappe.throw(
                "No active FB Recipe was found for item {0} in company {1}".format(
                    item_code, self.company
                ),
                frappe.ValidationError,
            )

        if len(effective_candidates) > 1:
            frappe.throw(
                "Multiple active FB Recipes were found for item {0}: {1}".format(
                    item_code, ", ".join(recipe.name for recipe in effective_candidates)
                ),
                frappe.ValidationError,
            )

        return effective_candidates[0]

    def recipe_is_effective(self, recipe_doc: DocumentLike) -> bool:
        submit_time = now_datetime()
        if recipe_doc.effective_from and submit_time < recipe_doc.effective_from:
            return False
        if recipe_doc.effective_to and submit_time > recipe_doc.effective_to:
            return False
        return True

    def validate_modifier_selections(
        self, line_index: int, line, recipe_doc: DocumentLike
    ) -> list[dict[str, Any]]:
        allowed_group_rows = recipe_doc.get("allowed_modifier_groups") or []
        all_allowed_group_map = {
            row.modifier_group: row for row in allowed_group_rows if row.modifier_group
        }
        selected_modifiers = []
        selections_by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        selected_rows = self.get_selected_modifier_rows(line)
        selected_modifier_names = {
            cstr(selected_row.modifier)
            for selected_row in selected_rows
            if cstr(selected_row.modifier)
        }
        visible_group_rows = filter_visible_allowed_modifier_groups(
            allowed_group_rows, selected_modifier_names
        )
        visible_group_map = {
            group_name: row
            for row in visible_group_rows
            if (group_name := cstr(getattr(row, "modifier_group", None)))
        }

        for selected_row in selected_rows:
            if not selected_row.modifier_group or not selected_row.modifier:
                frappe.throw(
                    "Order line {0} has a selected modifier row without modifier_group or modifier".format(
                        self.describe_line(line_index, line)
                    ),
                    frappe.ValidationError,
                )

            if selected_row.modifier_group not in all_allowed_group_map:
                frappe.throw(
                    "Order line {0} selected modifier group {1} is not allowed by recipe {2}".format(
                        self.describe_line(line_index, line),
                        selected_row.modifier_group,
                        recipe_doc.name,
                    ),
                    frappe.ValidationError,
                )

            modifier_group_doc = frappe.get_cached_doc(
                "FB Modifier Group", selected_row.modifier_group
            )
            modifier_doc = frappe.get_cached_doc("FB Modifier", selected_row.modifier)

            if not int(modifier_group_doc.active):
                frappe.throw(
                    "Order line {0} selected inactive modifier group {1}".format(
                        self.describe_line(line_index, line), modifier_group_doc.name
                    ),
                    frappe.ValidationError,
                )

            if not int(modifier_doc.active):
                frappe.throw(
                    "Order line {0} selected inactive modifier {1}".format(
                        self.describe_line(line_index, line), modifier_doc.name
                    ),
                    frappe.ValidationError,
                )

            if modifier_doc.modifier_group != selected_row.modifier_group:
                frappe.throw(
                    "Order line {0} modifier {1} does not belong to modifier group {2}".format(
                        self.describe_line(line_index, line),
                        modifier_doc.name,
                        selected_row.modifier_group,
                    ),
                    frappe.ValidationError,
                )

            selected_row.price_adjustment = flt(modifier_doc.price_adjustment)
            selected_row.instruction_text = (
                selected_row.instruction_text or modifier_doc.instruction_text
            )
            selected_row.sort_order = (
                selected_row.sort_order or modifier_doc.display_order or 0
            )
            selected_row.affects_stock = int(modifier_doc.affects_stock)
            selected_row.affects_recipe = int(modifier_doc.affects_recipe)

            normalized_modifier = {
                "row": selected_row,
                "group_row": all_allowed_group_map[selected_row.modifier_group],
                "group_doc": modifier_group_doc,
                "modifier_doc": modifier_doc,
            }
            selections_by_group[selected_row.modifier_group].append(normalized_modifier)
            selected_modifiers.append(normalized_modifier)

        for group_name, group_row in visible_group_map.items():
            group_doc = frappe.get_cached_doc("FB Modifier Group", group_name)
            selected_count = len(selections_by_group.get(group_name, []))
            min_selection = self.resolve_min_selection(group_doc, group_row)
            max_selection = self.resolve_max_selection(group_doc, group_row)

            if group_doc.selection_type == "Single" and selected_count > 1:
                frappe.throw(
                    "Order line {0} modifier group {1} allows only one selection".format(
                        self.describe_line(line_index, line), group_name
                    ),
                    frappe.ValidationError,
                )

            if min_selection and selected_count < min_selection:
                frappe.throw(
                    "Order line {0} modifier group {1} requires at least {2} selection(s)".format(
                        self.describe_line(line_index, line), group_name, min_selection
                    ),
                    frappe.ValidationError,
                )

            if max_selection and selected_count > max_selection:
                frappe.throw(
                    "Order line {0} modifier group {1} allows at most {2} selection(s)".format(
                        self.describe_line(line_index, line), group_name, max_selection
                    ),
                    frappe.ValidationError,
                )

        return selected_modifiers

    def resolve_min_selection(self, group_doc: DocumentLike, group_row) -> int:
        if group_row.override_min_selection is not None:
            return int(group_row.override_min_selection or 0)
        if int(group_row.required):
            if group_doc.selection_type == "Single":
                return 1
            return int(group_doc.min_selection or 1)
        if int(group_doc.is_required):
            if group_doc.selection_type == "Single":
                return 1
            return int(group_doc.min_selection or 1)
        return int(group_doc.min_selection or 0)

    def resolve_max_selection(self, group_doc: DocumentLike, group_row) -> int:
        if group_row.override_max_selection is not None:
            return int(group_row.override_max_selection or 0)
        if group_doc.selection_type == "Single":
            return 1
        return int(group_doc.max_selection or 0)

    def resolve_components_for_line(
        self,
        line_index: int,
        line,
        recipe_doc: DocumentLike,
        selected_modifiers: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        scale_factor = flt(line.qty) / flt(recipe_doc.default_serving_qty or 1)
        resolved_components = []

        for component_index, component_row in enumerate(recipe_doc.components, start=1):
            resolved_components.append(
                {
                    "item": component_row.item,
                    "source_type": "Base Recipe",
                    "qty": flt(component_row.qty) * scale_factor,
                    "uom": component_row.uom,
                    "stock_qty": flt(component_row.stock_qty or component_row.qty)
                    * scale_factor,
                    "stock_uom": component_row.stock_uom or component_row.uom,
                    "warehouse": self.booth_warehouse,
                    "source_reference": component_row.substitution_key
                    or "component-{0}".format(component_index),
                    "affects_stock": int(component_row.affects_stock),
                    "affects_cogs": int(component_row.affects_cogs),
                    "remarks": component_row.remarks,
                }
            )

        for selected_modifier in selected_modifiers:
            modifier_doc = selected_modifier["modifier_doc"]
            if not int(modifier_doc.affects_recipe):
                continue

            if modifier_doc.kind == "Add":
                resolved_components.append(
                    self.build_modifier_component(modifier_doc, line_index, line)
                )
                continue

            matched_components = self.find_matching_components(
                resolved_components=resolved_components,
                modifier_doc=modifier_doc,
            )

            if not matched_components:
                frappe.throw(
                    "Order line {0} modifier {1} could not resolve a target recipe component".format(
                        self.describe_line(line_index, line), modifier_doc.name
                    ),
                    frappe.ValidationError,
                )

            if modifier_doc.kind == "Remove":
                for component in matched_components:
                    resolved_components.remove(component)
                continue

            if modifier_doc.kind == "Replace":
                if not modifier_doc.new_item:
                    frappe.throw(
                        "Order line {0} modifier {1} requires new_item for replacement".format(
                            self.describe_line(line_index, line), modifier_doc.name
                        ),
                        frappe.ValidationError,
                    )
                for component in matched_components:
                    component["item"] = modifier_doc.new_item
                    component["source_type"] = "Modifier Replace"
                    component["source_reference"] = modifier_doc.name
                    if modifier_doc.qty_delta:
                        component["qty"] = flt(component["qty"]) + flt(
                            modifier_doc.qty_delta
                        )
                        component["stock_qty"] = flt(component["stock_qty"]) + flt(
                            modifier_doc.qty_delta
                        )
                continue

            if modifier_doc.kind == "Scale":
                scale_percent = flt(modifier_doc.scale_percent)
                if scale_percent <= 0:
                    frappe.throw(
                        "Order line {0} modifier {1} requires scale_percent greater than 0".format(
                            self.describe_line(line_index, line), modifier_doc.name
                        ),
                        frappe.ValidationError,
                    )
                scale_multiplier = scale_percent / 100
                for component in matched_components:
                    component["qty"] = flt(component["qty"]) * scale_multiplier
                    component["stock_qty"] = (
                        flt(component["stock_qty"]) * scale_multiplier
                    )
                    component["source_type"] = "Modifier Scale"
                    component["source_reference"] = modifier_doc.name
                continue

            frappe.throw(
                "Order line {0} modifier {1} has unsupported recipe effect kind {2}".format(
                    self.describe_line(line_index, line),
                    modifier_doc.name,
                    modifier_doc.kind,
                ),
                frappe.ValidationError,
            )

        if not resolved_components:
            frappe.throw(
                "Order line {0} resolved to zero components".format(
                    self.describe_line(line_index, line)
                ),
                frappe.ValidationError,
            )

        return resolved_components

    def build_modifier_component(
        self, modifier_doc: DocumentLike, line_index: int, line
    ) -> dict[str, Any]:
        target_item = modifier_doc.new_item or modifier_doc.target_item
        if not target_item:
            frappe.throw(
                "Order line {0} modifier {1} requires target_item or new_item for Add resolution".format(
                    self.describe_line(line_index, line), modifier_doc.name
                ),
                frappe.ValidationError,
            )
        if not modifier_doc.qty_uom:
            frappe.throw(
                "Order line {0} modifier {1} requires qty_uom for Add resolution".format(
                    self.describe_line(line_index, line), modifier_doc.name
                ),
                frappe.ValidationError,
            )
        if flt(modifier_doc.qty_delta) <= 0:
            frappe.throw(
                "Order line {0} modifier {1} requires qty_delta greater than 0 for Add resolution".format(
                    self.describe_line(line_index, line), modifier_doc.name
                ),
                frappe.ValidationError,
            )

        return {
            "item": target_item,
            "source_type": "Modifier Add",
            "qty": flt(modifier_doc.qty_delta),
            "uom": modifier_doc.qty_uom,
            "stock_qty": flt(modifier_doc.qty_delta),
            "stock_uom": modifier_doc.qty_uom,
            "warehouse": self.booth_warehouse,
            "source_reference": modifier_doc.name,
            "affects_stock": int(modifier_doc.affects_stock or 1),
            "affects_cogs": 1,
            "remarks": modifier_doc.instruction_text,
        }

    def find_matching_components(
        self, resolved_components: list[dict[str, Any]], modifier_doc: DocumentLike
    ) -> list[dict[str, Any]]:
        if modifier_doc.target_substitution_key:
            return [
                component
                for component in resolved_components
                if component.get("source_reference")
                == modifier_doc.target_substitution_key
            ]
        if modifier_doc.target_item:
            return [
                component
                for component in resolved_components
                if component.get("item") == modifier_doc.target_item
            ]
        return []

    def validate_stock_availability(self, line_resolutions: list[dict[str, Any]]):
        required_stock_by_bin: dict[tuple[str, str], float] = defaultdict(float)

        for line_resolution in line_resolutions:
            for component in line_resolution["resolved_components"]:
                if not int(component.get("affects_stock") or 0):
                    continue
                item_code = component.get("item")
                warehouse = component.get("warehouse") or self.booth_warehouse
                required_stock_by_bin[(item_code, warehouse)] += flt(
                    component.get("stock_qty")
                )

        for (item_code, warehouse), required_stock in required_stock_by_bin.items():
            available_stock = flt(
                frappe.db.get_value(
                    "Bin",
                    {"item_code": item_code, "warehouse": warehouse},
                    "actual_qty",
                )
                or 0
            )
            if available_stock + 0.0001 < required_stock:
                frappe.throw(
                    "Insufficient stock for item {0} in warehouse {1}. Required {2}, available {3}".format(
                        item_code,
                        warehouse,
                        required_stock,
                        available_stock,
                    ),
                    frappe.ValidationError,
                )

    def create_resolved_sales(self, line_resolutions: list[dict[str, Any]]):
        for line_resolution in line_resolutions:
            line = line_resolution["line"]
            recipe_doc = line_resolution["recipe_doc"]
            selected_modifiers = line_resolution["selected_modifiers"]
            resolved_components = line_resolution["resolved_components"]
            resolved_sale_id = line.backend_line_uuid or "{0}-{1}".format(
                self.order_id, line.line_id
            )

            existing_resolved_sale = frappe.db.get_value(
                "FB Resolved Sale", {"resolved_sale_id": resolved_sale_id}, "name"
            )
            if existing_resolved_sale:
                frappe.throw(
                    "Resolved sale {0} already exists as {1}".format(
                        resolved_sale_id, existing_resolved_sale
                    ),
                    frappe.ValidationError,
                )

            resolved_sale = frappe.new_doc("FB Resolved Sale")
            resolved_sale.resolved_sale_id = resolved_sale_id
            resolved_sale.fb_order = self.name
            resolved_sale.fb_order_line = line.name
            resolved_sale.backend_line_uuid = line.backend_line_uuid
            resolved_sale.sellable_item = line.item
            resolved_sale.qty = line.qty
            resolved_sale.booth_warehouse = self.booth_warehouse
            resolved_sale.recipe = recipe_doc.name
            resolved_sale.recipe_version = recipe_doc.version_no
            resolved_sale.status = "Submitted"
            resolved_sale.event_project = self.event_project
            resolved_sale.resolution_hash = self.build_resolution_hash(
                recipe_doc=recipe_doc,
                selected_modifiers=selected_modifiers,
                resolved_components=resolved_components,
            )

            for selected_modifier in selected_modifiers:
                selected_row = selected_modifier["row"]
                resolved_sale.append(
                    "selected_modifiers",
                    {
                        "modifier_group": selected_row.modifier_group,
                        "modifier": selected_row.modifier,
                        "price_adjustment": selected_row.price_adjustment,
                        "instruction_text": selected_row.instruction_text,
                        "sort_order": selected_row.sort_order,
                        "affects_stock": selected_row.affects_stock,
                        "affects_recipe": selected_row.affects_recipe,
                    },
                )

            for component in resolved_components:
                resolved_sale.append("resolved_components", component)

            resolved_sale.insert(ignore_permissions=True)
            line.resolved_sale = resolved_sale.name
            line.resolved_components_snapshot = json.dumps(
                resolved_components, sort_keys=True, default=str
            )

    def build_resolution_hash(
        self,
        recipe_doc: DocumentLike,
        selected_modifiers: list[dict[str, Any]],
        resolved_components: list[dict[str, Any]],
    ) -> str:
        payload = {
            "recipe": recipe_doc.name,
            "recipe_version": recipe_doc.version_no,
            "selected_modifiers": [
                {
                    "modifier_group": entry["row"].modifier_group,
                    "modifier": entry["row"].modifier,
                    "price_adjustment": flt(entry["row"].price_adjustment),
                }
                for entry in selected_modifiers
            ],
            "resolved_components": resolved_components,
        }
        serialized_payload = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()

    def run_projection_service(
        self, candidate_paths: list[str], projection_label: str
    ) -> Any:
        last_lookup_error = None
        callable_service = None
        selected_path = None

        for candidate_path in candidate_paths:
            try:
                callable_service = frappe.get_attr(candidate_path)
                selected_path = candidate_path
                break
            except Exception as error:
                last_lookup_error = error

        if callable_service is None:
            raise frappe.ValidationError(
                "Unable to resolve {0} projection service. Tried: {1}. Last error: {2}".format(
                    projection_label,
                    ", ".join(candidate_paths),
                    last_lookup_error,
                )
            )

        try:
            return callable_service(self)
        except Exception as error:
            if projection_label == "invoice":
                self.invoice_status = "Failed"
            elif projection_label == "stock":
                self.stock_status = "Failed"
            raise frappe.ValidationError(
                "{0} projection service {1} failed for FB Order {2}: {3}".format(
                    projection_label.capitalize(), selected_path, self.name, error
                )
            ) from error

    def extract_target_name(self, projection_result: Any) -> str | None:
        if isinstance(projection_result, str):
            return projection_result
        if isinstance(projection_result, dict):
            for key in (
                "name",
                "target_name",
                "sales_invoice",
                "stock_entry",
                "ingredient_stock_entry",
            ):
                value = projection_result.get(key)
                if value:
                    return value
        return None

    def describe_line(self, line_index: int, line) -> str:
        return line.line_id or line.backend_line_uuid or str(line_index)
