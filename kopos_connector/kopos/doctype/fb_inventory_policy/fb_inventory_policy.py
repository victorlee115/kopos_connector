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
                "The stock tracking start reference and start time must be saved together",
                frappe.ValidationError,
            )
        if cstr(getattr(self, "automation_state", None)).strip() == "Active" and not token:
            frappe.throw(
                "Stock suggestions cannot be turned on until the starting stock count is saved",
                frappe.ValidationError,
            )
        if cstr(getattr(self, "automation_state", None)).strip() == "Active" and not cstr(getattr(self, "opening_stock_reconciliation", None)).strip():
            frappe.throw(
                "Stock suggestions cannot be turned on until the approved starting stock count is submitted",
                frappe.ValidationError,
            )
        max_age = int(getattr(self, "max_source_age_minutes", 30) or 30)
        if max_age < 1 or max_age > 24 * 60:
            frappe.throw("Stock information must be allowed to be between 1 minute and 24 hours old", frappe.ValidationError)
        preparation_device = cstr(getattr(self, "preparation_device", None)).strip()
        if preparation_device:
            device = frappe.db.get_value(
                "KoPOS Device",
                preparation_device,
                ["pos_profile", "enabled"],
                as_dict=True,
            )
            if not device or not bool(device.get("enabled")):
                frappe.throw("The tablet used for preparation must be turned on in JiJi settings", frappe.ValidationError)
            profile = frappe.db.get_value(
                "POS Profile",
                device.get("pos_profile"),
                ["company", "warehouse"],
                as_dict=True,
            ) or {}
            if cstr(profile.get("company")).strip() != cstr(self.company).strip() or cstr(profile.get("warehouse")).strip() != cstr(self.warehouse).strip():
                frappe.throw("The preparation tablet must belong to this company and stock location", frappe.ValidationError)
        percent_ceiling = getattr(self, "count_variance_percent_ceiling", None)
        if percent_ceiling not in (None, ""):
            percent_value = _finite_decimal(
                percent_ceiling,
                "Largest allowed count difference",
            )
            if percent_value < 0 or percent_value > 100:
                frappe.throw("The largest allowed count difference must be between 0% and 100%", frappe.ValidationError)
        value_ceiling = getattr(self, "count_variance_value_ceiling", None)
        if value_ceiling not in (None, ""):
            value = _finite_decimal(
                value_ceiling,
                "Largest allowed count value difference",
            )
            if value < 0:
                frappe.throw("The largest allowed count value difference must be zero or more", frappe.ValidationError)
        preparation_percent_ceiling = getattr(self, "preparation_variance_percent_ceiling", None)
        if preparation_percent_ceiling not in (None, ""):
            preparation_value = _finite_decimal(
                preparation_percent_ceiling,
                "Largest allowed preparation difference",
            )
            if preparation_value < 0 or preparation_value > 100:
                frappe.throw("The largest allowed preparation difference must be between 0% and 100%", frappe.ValidationError)
        automation_user = cstr(getattr(self, "automation_user", None)).strip()
        if automation_user:
            if automation_user == "Administrator" or not frappe.db.get_value("User", automation_user, "enabled"):
                frappe.throw("Choose an enabled staff account for automatic stock work, not Administrator", frappe.ValidationError)
            roles = set(frappe.get_roles(automation_user))
            if roles.intersection({"System Manager", "Company Director"}):
                frappe.throw("The account used for automatic stock work cannot be a Company Director or System Manager", frappe.ValidationError)
        review_owner = cstr(getattr(self, "purchase_review_owner", None)).strip()
        if review_owner:
            if not frappe.db.get_value("User", review_owner, "enabled"):
                frappe.throw("Choose an enabled director to review supplier order drafts", frappe.ValidationError)
            if "Company Director" not in set(frappe.get_roles(review_owner)):
                frappe.throw("The person who reviews supplier order drafts must have the Company Director role", frappe.ValidationError)
        exception_owner = cstr(getattr(self, "inventory_exception_owner", None)).strip()
        if exception_owner:
            if exception_owner in {"Administrator", "Guest"} or not frappe.db.get_value("User", exception_owner, "enabled"):
                frappe.throw("Choose an enabled staff account to handle stock problems", frappe.ValidationError)
            if "Company Director" not in set(frappe.get_roles(exception_owner)):
                frappe.throw("The person who handles stock problems must have the Company Director role", frappe.ValidationError)

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
                "The stock tracking start record cannot be changed after activation. Changed fields: "
                + ", ".join(changed),
                frappe.ValidationError,
            )
