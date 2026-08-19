"""Least-privilege identity boundary for unattended inventory documents."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Iterable

import frappe
from frappe.utils import cstr


@dataclass(frozen=True)
class AutomationIdentityError(Exception):
    """A configured automation user cannot safely perform the requested work."""

    reason: str


def automation_identity_is_configured(*, company: str, warehouse: str) -> bool:
    """Return whether a non-privileged, enabled automation user is configured."""

    user, _ = _configured_user(company=company, warehouse=warehouse)
    return bool(user)


def purchase_review_owner(*, company: str, warehouse: str) -> str | None:
    """Return the configured Company Director who receives Draft PO review work."""

    policy = _policy(company=company, warehouse=warehouse)
    owner = cstr(policy.get("purchase_review_owner")).strip()
    if not owner or not _enabled_user(owner):
        return None
    try:
        if "Company Director" not in set(frappe.get_roles(owner)):
            return None
    except Exception:
        return None
    return owner


@contextmanager
def inventory_automation_identity(
    *,
    company: str,
    warehouse: str,
    create_doctypes: Iterable[str],
    submit_doctypes: Iterable[str] = (),
) -> Iterator[str]:
    """Execute a document mutation under the exact configured ERP user.

    The configured user must not be a Company Director, System Manager, or
    Administrator.  We then check Frappe's real permissions immediately before
    mutation; role names are deliberately not hard-coded because standard
    ERPNext permissions may be tailored by site.
    """

    user, reason = _configured_user(company=company, warehouse=warehouse)
    if not user:
        raise AutomationIdentityError(reason)
    previous_user = cstr(getattr(frappe.session, "user", None)).strip()
    try:
        frappe.set_user(user)
        for doctype in create_doctypes:
            if not frappe.has_permission(doctype, ptype="create"):
                raise AutomationIdentityError(f"Automation User lacks create permission for {doctype}")
        for doctype in submit_doctypes:
            if not frappe.has_permission(doctype, ptype="submit"):
                raise AutomationIdentityError(f"Automation User lacks submit permission for {doctype}")
        yield user
    finally:
        if previous_user:
            frappe.set_user(previous_user)


def _configured_user(*, company: str, warehouse: str) -> tuple[str | None, str]:
    policy = _policy(company=company, warehouse=warehouse)
    user = cstr(policy.get("automation_user")).strip()
    if not user:
        return None, "Configure a dedicated Automation User on this Inventory Policy"
    if user == "Administrator" or not _enabled_user(user):
        return None, "Automation User must be an enabled non-Administrator user"
    try:
        roles = set(frappe.get_roles(user))
    except Exception:
        return None, "Automation User roles could not be verified"
    prohibited = {"System Manager", "Company Director"}
    if roles & prohibited:
        return None, "Automation User must not hold Company Director or System Manager"
    return user, ""


def _policy(*, company: str, warehouse: str) -> dict[str, Any]:
    if not company or not warehouse or not frappe.db.exists("DocType", "FB Inventory Policy"):
        return {}
    meta = frappe.get_meta("FB Inventory Policy")
    fields = [field for field in ("automation_user", "purchase_review_owner") if meta.has_field(field)]
    if not fields:
        return {}
    return frappe.db.get_value(
        "FB Inventory Policy",
        {"company": company, "warehouse": warehouse},
        fields,
        as_dict=True,
    ) or {}


def _enabled_user(user: str) -> bool:
    try:
        return bool(frappe.db.get_value("User", user, "enabled"))
    except Exception:
        return False
