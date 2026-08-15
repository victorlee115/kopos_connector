from decimal import Decimal, InvalidOperation

import frappe
from frappe.model.document import Document


class FBModifier(Document):
    def validate(self) -> None:
        self.populate_exact_decimal_fields()
        self.validate_new_published_group_effect_is_not_added()
        self.validate_used_operational_definition_is_immutable()

    def populate_exact_decimal_fields(self) -> None:
        self.qty_delta_decimal = _canonical_input(
            self, "qty_delta_decimal", "qty_delta", positive=True
        )
        self.scale_percent_decimal = _canonical_input(
            self, "scale_percent_decimal", "scale_percent", positive=False
        )

    def validate_new_published_group_effect_is_not_added(self) -> None:
        """A published recipe's selectable effects are frozen at publication.

        The cashier wire model currently shares one modifier group across menu
        items. Refusing a newly active option in a group used by an inventory
        recipe with a canonical snapshot keeps an old tablet from offering an
        effect that its sale-time recipe snapshot never approved. Legacy
        pre-cutover recipes deliberately have no canonical snapshot and remain
        commercially compatible; they are never inventory-projected.
        """

        if not bool(getattr(self, "active", 0)):
            return
        is_new = getattr(self, "is_new", None)
        previous = self.get_doc_before_save() if hasattr(self, "get_doc_before_save") else None
        is_becoming_active = bool(previous and not bool(getattr(previous, "active", 0)))
        if not ((callable(is_new) and is_new()) or is_becoming_active):
            return
        group_name = getattr(self, "modifier_group", None)
        if not group_name:
            return
        if _modifier_group_has_active_recipe(group_name):
            frappe.throw(
                "FB Modifier {0} cannot become active in a group used by a published recipe; "
                "create a new modifier group and publish a new recipe version".format(
                    getattr(self, "name", None) or getattr(self, "modifier_code", None)
                ),
                frappe.ValidationError,
            )

    def validate_used_operational_definition_is_immutable(self) -> None:
        is_new = getattr(self, "is_new", None)
        if (callable(is_new) and is_new()) or not getattr(self, "name", None):
            return
        get_before_save = getattr(self, "get_doc_before_save", None)
        if not callable(get_before_save):
            return
        previous = get_before_save()
        if not previous:
            return
        is_published = bool(getattr(previous, "active", 0))
        if not is_published and not _modifier_is_used(self.name):
            return
        immutable_fields = (
            "modifier_group",
            "kind",
            "target_substitution_key",
            "target_item",
            "new_item",
            "qty_delta",
            "qty_uom",
            "scale_percent",
            "affects_stock",
            "affects_recipe",
            "is_default",
        )
        changed_fields = [
            fieldname
            for fieldname in immutable_fields
            if getattr(previous, fieldname, None) != getattr(self, fieldname, None)
        ]
        if changed_fields:
            frappe.throw(
                "FB Modifier {0} has already been published or used by a sale; create a new modifier instead of changing operational field(s): {1}".format(
                    self.name,
                    ", ".join(changed_fields),
                ),
                frappe.ValidationError,
            )

    def before_rename(self, old: str, new: str, merge: bool = False) -> None:
        del new, merge
        if bool(getattr(self, "active", 0)) or _modifier_is_used(old):
            frappe.throw(
                f"Published or used FB Modifier {old} cannot be renamed; create a new modifier",
                frappe.ValidationError,
            )

    def on_trash(self) -> None:
        if bool(getattr(self, "active", 0)) or _modifier_is_used(self.name):
            frappe.throw(
                f"Published or used FB Modifier {self.name} cannot be deleted",
                frappe.ValidationError,
            )


def _modifier_is_used(modifier_name: str) -> bool:
    return bool(
        frappe.db.exists("FB Selected Modifier", {"modifier": modifier_name})
    )


def _modifier_group_has_active_recipe(group_name: str) -> bool:
    recipe_names = frappe.get_all(
        "FB Allowed Modifier Group",
        filters={
            "modifier_group": group_name,
            "parenttype": "FB Recipe",
            "parentfield": "allowed_modifier_groups",
        },
        pluck="parent",
    )
    return bool(
        recipe_names
        and frappe.db.exists(
            "FB Recipe",
            {
                "status": "Active",
                "name": ["in", recipe_names],
                "canonical_hash": ["!=", ""],
            },
        )
    )


def _canonical_input(
    value: object, canonical_field: str, legacy_field: str, *, positive: bool
) -> str | None:
    exact = getattr(value, canonical_field, None)
    raw = exact if exact not in (None, "") else getattr(value, legacy_field, None)
    if raw in (None, ""):
        return None
    try:
        parsed = Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        return str(raw).strip() or None
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        return str(raw).strip() or None
    normalized = parsed.normalize()
    return format(normalized, "f") if normalized != 0 else "0"
