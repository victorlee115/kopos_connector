import frappe
from frappe.model.document import Document


class FBModifier(Document):
    def validate(self) -> None:
        self.validate_used_operational_definition_is_immutable()

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
