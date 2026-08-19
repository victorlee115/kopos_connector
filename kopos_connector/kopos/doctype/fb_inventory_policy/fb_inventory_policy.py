from __future__ import annotations

from decimal import Decimal, InvalidOperation

import frappe
from frappe.model.document import Document
from frappe.utils import cstr


def _finite_decimal(value: object, label: str) -> Decimal:
    """Parse a policy ceiling without allowing float rounding or NaN."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        frappe.throw(f"{label} must be a finite decimal", frappe.ValidationError)
        raise AssertionError("frappe.throw must raise") from error
    if not parsed.is_finite():
        frappe.throw(f"{label} must be a finite decimal", frappe.ValidationError)
    return parsed


class FBInventoryPolicy(Document):
    """Own one outlet's cutover identity and automation lifecycle."""

    _CUTOVER_FIELDS = (
        "company",
        "warehouse",
        "inventory_contract_version",
        "cutover_token",
        "cutover_at",
        "opening_stock_reconciliation",
    )

    # Ceilings that decide whether automation may raise a Material Request or a
    # Draft Purchase Order. Frappe stores each as Float/Currency; the paired
    # hidden *_decimal text is the authority planning compares against, matching
    # how recipe components and plan lines already carry exact quantities.
    _DECIMAL_CEILING_FIELDS = (
        ("quantity_ceiling", "Quantity ceiling"),
        ("value_ceiling", "Value ceiling"),
        ("count_variance_percent_ceiling", "Count variance percentage ceiling"),
        ("count_variance_value_ceiling", "Count variance value ceiling"),
    )

    def _sync_decimal_ceilings(self) -> None:
        """Keep an exact decimal text beside each numeric automation ceiling.

        An operator-supplied exact text is preserved when it still represents the
        same number, so editing an unrelated field never silently rewrites a
        director's precise ceiling through a float round trip.
        """

        for fieldname, label in self._DECIMAL_CEILING_FIELDS:
            decimal_field = f"{fieldname}_decimal"
            raw = getattr(self, fieldname, None)
            if raw in (None, ""):
                setattr(self, decimal_field, None)
                continue
            parsed = _finite_decimal(raw, label)
            stored_text = cstr(getattr(self, decimal_field, None)).strip()
            if stored_text:
                try:
                    if Decimal(stored_text) == parsed:
                        continue
                except (InvalidOperation, ValueError):
                    pass
            setattr(self, decimal_field, format(parsed, "f"))

    def validate(self) -> None:
        token = cstr(getattr(self, "cutover_token", None)).strip()
        cutover_at = getattr(self, "cutover_at", None)
        if bool(token) != bool(cutover_at):
            frappe.throw(
                "Inventory cutover token and cutover time must be provided together",
                frappe.ValidationError,
            )
        if cstr(getattr(self, "automation_state", None)).strip() == "Active" and not token:
            frappe.throw(
                "Inventory automation cannot be Active before an immutable cutover is recorded",
                frappe.ValidationError,
            )
        if cstr(getattr(self, "automation_state", None)).strip() == "Active" and not cstr(getattr(self, "opening_stock_reconciliation", None)).strip():
            frappe.throw(
                "Inventory automation cannot be Active before the opening Stock Reconciliation is submitted",
                frappe.ValidationError,
            )
        max_age = int(getattr(self, "max_source_age_minutes", 30) or 30)
        if max_age < 1 or max_age > 24 * 60:
            frappe.throw("Maximum source age must be between 1 and 1440 minutes", frappe.ValidationError)
        preparation_device = cstr(getattr(self, "preparation_device", None)).strip()
        if preparation_device:
            device = frappe.db.get_value(
                "KoPOS Device",
                preparation_device,
                ["pos_profile", "enabled"],
                as_dict=True,
            )
            if not device or not bool(device.get("enabled")):
                frappe.throw("Preparation Device must be an enabled KoPOS Device", frappe.ValidationError)
            profile = frappe.db.get_value(
                "POS Profile",
                device.get("pos_profile"),
                ["company", "warehouse"],
                as_dict=True,
            ) or {}
            if cstr(profile.get("company")).strip() != cstr(self.company).strip() or cstr(profile.get("warehouse")).strip() != cstr(self.warehouse).strip():
                frappe.throw("Preparation Device must belong to this policy company and warehouse", frappe.ValidationError)
        percent_ceiling = getattr(self, "count_variance_percent_ceiling", None)
        if percent_ceiling not in (None, ""):
            percent_value = _finite_decimal(
                percent_ceiling,
                "Count variance percentage ceiling",
            )
            if percent_value < 0 or percent_value > 100:
                frappe.throw("Count variance percentage ceiling must be between 0 and 100", frappe.ValidationError)
        value_ceiling = getattr(self, "count_variance_value_ceiling", None)
        if value_ceiling not in (None, ""):
            value = _finite_decimal(
                value_ceiling,
                "Count variance value ceiling",
            )
            if value < 0:
                frappe.throw("Count variance value ceiling must be a non-negative amount", frappe.ValidationError)
        preparation_percent_ceiling = getattr(self, "preparation_variance_percent_ceiling", None)
        if preparation_percent_ceiling not in (None, ""):
            preparation_value = _finite_decimal(
                preparation_percent_ceiling,
                "Preparation variance percentage ceiling",
            )
            if preparation_value < 0 or preparation_value > 100:
                frappe.throw("Preparation variance percentage ceiling must be between 0 and 100", frappe.ValidationError)
        automation_user = cstr(getattr(self, "automation_user", None)).strip()
        if automation_user:
            if automation_user == "Administrator" or not frappe.db.get_value("User", automation_user, "enabled"):
                frappe.throw("Automation User must be an enabled non-Administrator user", frappe.ValidationError)
            roles = set(frappe.get_roles(automation_user))
            if roles.intersection({"System Manager", "Company Director"}):
                frappe.throw("Automation User must not hold Company Director or System Manager", frappe.ValidationError)
        review_owner = cstr(getattr(self, "purchase_review_owner", None)).strip()
        if review_owner:
            if not frappe.db.get_value("User", review_owner, "enabled"):
                frappe.throw("Draft PO Review Owner must be an enabled user", frappe.ValidationError)
            if "Company Director" not in set(frappe.get_roles(review_owner)):
                frappe.throw("Draft PO Review Owner must hold Company Director", frappe.ValidationError)
        exception_owner = cstr(getattr(self, "inventory_exception_owner", None)).strip()
        if exception_owner:
            if exception_owner in {"Administrator", "Guest"} or not frappe.db.get_value("User", exception_owner, "enabled"):
                frappe.throw("Inventory Exception Owner must be an enabled non-system user", frappe.ValidationError)
            if "Company Director" not in set(frappe.get_roles(exception_owner)):
                frappe.throw("Inventory Exception Owner must hold Company Director", frappe.ValidationError)

        self._sync_decimal_ceilings()

        before = self.get_doc_before_save()
        previous_token = cstr(getattr(before, "cutover_token", None)).strip() if before else ""
        if not previous_token:
            return
        changed = [
            fieldname
            for fieldname in self._CUTOVER_FIELDS
            if str(getattr(before, fieldname, None) or "")
            != str(getattr(self, fieldname, None) or "")
        ]
        if changed:
            frappe.throw(
                "Activated inventory cutover identity is immutable; changed fields: "
                + ", ".join(changed),
                frappe.ValidationError,
            )
