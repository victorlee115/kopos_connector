from __future__ import annotations

from importlib import import_module
from typing import Collection, Sequence

frappe = import_module("frappe")
Document = import_module("frappe.model.document").Document


def cstr(value: object) -> str:
    return str(frappe.utils.cstr(value)).strip()


def _get_group_identifier(group_doc: object) -> str:
    return cstr(
        getattr(group_doc, "name", None) or getattr(group_doc, "group_code", None)
    )


def _get_modifier_doc(modifier_name: str) -> object:
    return frappe.get_cached_doc("FB Modifier", modifier_name)


def _get_modifier_group_doc(group_name: str) -> object:
    return frappe.get_cached_doc("FB Modifier Group", group_name)


def _get_parent_group_name_from_modifier(modifier_name: str) -> str:
    modifier_doc = _get_modifier_doc(modifier_name)
    parent_group_name = cstr(getattr(modifier_doc, "modifier_group", None))
    if not parent_group_name:
        frappe.throw(
            f"Parent Modifier {modifier_name} must belong to an FB Modifier Group",
            frappe.ValidationError,
        )

    return parent_group_name


def is_modifier_group_visible(
    group_doc: object, selected_modifier_names: Collection[object] | None
) -> bool:
    parent_modifier_name = cstr(getattr(group_doc, "parent_modifier", None))
    if not parent_modifier_name:
        return True

    selected_names = {
        cstr(modifier_name)
        for modifier_name in (selected_modifier_names or [])
        if cstr(modifier_name)
    }
    return parent_modifier_name in selected_names


def filter_visible_allowed_modifier_groups(
    allowed_group_rows: Sequence[object] | None,
    selected_modifier_names: Collection[object] | None,
) -> list[object]:
    visible_rows: list[object] = []
    for row in allowed_group_rows or []:
        group_name = cstr(getattr(row, "modifier_group", None))
        if not group_name:
            continue

        group_doc = _get_modifier_group_doc(group_name)
        if is_modifier_group_visible(group_doc, selected_modifier_names):
            visible_rows.append(row)

    return visible_rows


class FBModifierGroup(Document):
    def validate(self) -> None:
        self.validate_parent_modifier_dependency()

    def validate_parent_modifier_dependency(self) -> None:
        current_group_name = _get_group_identifier(self)
        parent_modifier_name = cstr(getattr(self, "parent_modifier", None))
        if not current_group_name or not parent_modifier_name:
            return

        traversed_groups = {current_group_name}
        next_group_name = _get_parent_group_name_from_modifier(parent_modifier_name)
        if next_group_name == current_group_name:
            frappe.throw(
                f"Parent Modifier {parent_modifier_name} must belong to another FB Modifier Group",
                frappe.ValidationError,
            )

        while next_group_name:
            parent_group_name = next_group_name
            if parent_group_name in traversed_groups:
                frappe.throw(
                    f"Circular parent modifier dependency detected for FB Modifier Group {current_group_name}",
                    frappe.ValidationError,
                )

            traversed_groups.add(parent_group_name)
            parent_group_doc = _get_modifier_group_doc(parent_group_name)
            next_modifier_name = cstr(
                getattr(parent_group_doc, "parent_modifier", None)
            )
            next_group_name = (
                _get_parent_group_name_from_modifier(next_modifier_name)
                if next_modifier_name
                else ""
            )
