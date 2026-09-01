# pyright: reportMissingImports=false

"""Fail-closed company and outlet scope for JiJi director pages.

Every beginner page uses this module before it reads business data. A user
default chooses between permitted values; it never grants access to one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import frappe
from frappe.utils import cstr


DIRECTOR_ROLE = "Company Director"
SYSTEM_MANAGER_ROLE = "System Manager"
CONFIGURATION_LIMIT = 500
BUSINESS_HOME_PAGE = "desk/jiji_home?sidebar=JiJi"


@dataclass(frozen=True)
class DirectorContext:
    user: str
    system_manager: bool
    allowed_companies: tuple[str, ...]
    company: str
    allowed_warehouses: tuple[str, ...]
    warehouse: str
    allowed_price_lists: tuple[str, ...]

    @property
    def status(self) -> str:
        if not self.company:
            return "selection_required" if self.allowed_companies else "permission_denied"
        if not self.warehouse and len(self.allowed_warehouses) > 1:
            return "selection_required"
        return "ready"


def _current_user(user: str | None = None) -> str:
    return cstr(user or getattr(frappe.session, "user", None)).strip()


def _roles(user: str) -> set[str]:
    return {cstr(role).strip() for role in frappe.get_roles(user) if cstr(role).strip()}


def business_home_page(user: str) -> str | None:
    """Land ordinary directors on JiJi without changing support accounts."""

    roles = _roles(_current_user(user))
    if DIRECTOR_ROLE in roles and SYSTEM_MANAGER_ROLE not in roles:
        return BUSINESS_HOME_PAGE
    return None


def require_director_role(user: str | None = None) -> tuple[str, bool]:
    resolved_user = _current_user(user)
    roles = _roles(resolved_user)
    system_manager = SYSTEM_MANAGER_ROLE in roles
    if not system_manager and DIRECTOR_ROLE not in roles:
        frappe.throw(
            frappe._("You do not have access to JiJi Director."),
            frappe.PermissionError,
        )
    return resolved_user, system_manager


def _user_default(key: str) -> str:
    defaults_api = getattr(frappe, "defaults", None)
    get_defaults = getattr(defaults_api, "get_defaults", None)
    if not callable(get_defaults):
        return ""
    values = get_defaults()
    value = values.get(key) if isinstance(values, dict) else None
    if isinstance(value, (list, tuple)):
        return cstr(value[0]).strip() if len(value) == 1 else ""
    return cstr(value or "").strip()


def _unique(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(sorted({cstr(value).strip() for value in values if cstr(value).strip()}))


def _permission_values(user: str, allow: str) -> tuple[str, ...]:
    return _unique(
        frappe.get_all(
            "User Permission",
            filters={"user": user, "allow": allow},
            pluck="for_value",
            limit_page_length=CONFIGURATION_LIMIT,
        )
    )


def allowed_companies(user: str | None = None) -> tuple[str, ...]:
    resolved_user, system_manager = require_director_role(user)
    if system_manager:
        return _unique(
            frappe.get_all(
                "Company",
                pluck="name",
                order_by="name asc",
                limit_page_length=CONFIGURATION_LIMIT,
            )
        )
    # Company Director is a business role, not tenant authority. A Company
    # User Permission is the explicit link between that person and the café.
    return _permission_values(resolved_user, "Company")


def _select_permitted(
    *,
    requested: str | None,
    default: str,
    allowed: tuple[str, ...],
    label: str,
) -> str:
    selected = cstr(requested).strip()
    if selected:
        if selected not in allowed:
            frappe.throw(
                frappe._("You do not have access to this {0}.").format(label),
                frappe.PermissionError,
            )
        return selected
    if default and default in allowed:
        return default
    return allowed[0] if len(allowed) == 1 else ""


def _profile_rows(company: str, user: str) -> list[dict[str, Any]]:
    if not company:
        return []
    warehouse_permissions = set(_permission_values(user, "Warehouse"))
    rows = frappe.get_all(
        "POS Profile",
        filters={"company": company, "disabled": 0},
        fields=["name", "warehouse", "selling_price_list"],
        order_by="name asc",
        limit_page_length=CONFIGURATION_LIMIT,
    )
    if not warehouse_permissions:
        return rows
    return [
        row
        for row in rows
        if cstr(row.get("warehouse")).strip() in warehouse_permissions
    ]


def resolve_director_context(
    *,
    company: str | None = None,
    warehouse: str | None = None,
    user: str | None = None,
) -> DirectorContext:
    resolved_user, system_manager = require_director_role(user)
    companies = allowed_companies(resolved_user)
    requested_warehouse = cstr(warehouse).strip()
    selected_company = _select_permitted(
        requested=company,
        default=_user_default("Company"),
        allowed=companies,
        label=frappe._("company"),
    )
    if not selected_company and requested_warehouse:
        warehouse_permissions = set(_permission_values(resolved_user, "Warehouse"))
        if warehouse_permissions and requested_warehouse not in warehouse_permissions:
            frappe.throw(
                frappe._("You do not have access to this outlet."),
                frappe.PermissionError,
            )
        matching_companies = _unique(
            row.get("company")
            for row in frappe.get_all(
                "POS Profile",
                filters={"warehouse": requested_warehouse, "disabled": 0},
                fields=["company"],
                limit_page_length=CONFIGURATION_LIMIT,
            )
            if cstr(row.get("company")).strip() in companies
        )
        if len(matching_companies) != 1:
            frappe.throw(
                frappe._("You do not have access to this outlet."),
                frappe.PermissionError,
            )
        selected_company = matching_companies[0]
    profiles = _profile_rows(selected_company, resolved_user)
    warehouses = _unique(row.get("warehouse") for row in profiles)
    selected_warehouse = _select_permitted(
        requested=requested_warehouse,
        default=_user_default("Warehouse"),
        allowed=warehouses,
        label=frappe._("outlet"),
    )
    price_profiles = (
        [row for row in profiles if cstr(row.get("warehouse")).strip() == selected_warehouse]
        if selected_warehouse
        else profiles
    )
    price_lists = _unique(row.get("selling_price_list") for row in price_profiles)
    if not price_lists and selected_company:
        fallback = cstr(
            frappe.db.get_single_value("Selling Settings", "selling_price_list") or ""
        ).strip()
        price_lists = (fallback,) if fallback else ()
    return DirectorContext(
        user=resolved_user,
        system_manager=system_manager,
        allowed_companies=companies,
        company=selected_company,
        allowed_warehouses=warehouses,
        warehouse=selected_warehouse,
        allowed_price_lists=price_lists,
    )


def director_company(requested: str | None = None) -> str:
    return resolve_director_context(company=requested).company


def director_warehouse(company: str, requested: str | None = None) -> str:
    return resolve_director_context(company=company, warehouse=requested).warehouse


def director_price_list(company: str) -> str:
    context = resolve_director_context(company=company)
    return context.allowed_price_lists[0] if len(context.allowed_price_lists) == 1 else ""


def director_pos_profiles(context: DirectorContext) -> tuple[str, ...]:
    """Enabled counters inside the already-authorized company/outlet scope."""
    if not context.company:
        return ()
    allowed_warehouses = set(context.allowed_warehouses)
    rows = frappe.get_all(
        "POS Profile",
        filters={"company": context.company, "disabled": 0},
        fields=["name", "warehouse"],
        order_by="name asc",
        limit_page_length=CONFIGURATION_LIMIT,
    )
    return _unique(
        row.get("name")
        for row in rows
        if cstr(row.get("warehouse")).strip() in allowed_warehouses
    )


def staff_assignment_companies(access: str) -> tuple[str, ...]:
    return _unique(
        frappe.get_all(
            "KoPOS Staff Outlet",
            filters={
                "parent": cstr(access).strip(),
                "parenttype": "KoPOS Staff Access",
            },
            pluck="company",
            limit_page_length=CONFIGURATION_LIMIT + 1,
        )
    )


def can_manage_staff_access(access: str, *, user: str | None = None) -> bool:
    context = resolve_director_context(user=user)
    assigned = staff_assignment_companies(access)
    if len(assigned) > CONFIGURATION_LIMIT:
        return False
    companies = set(assigned)
    return bool(companies) and (
        context.system_manager or companies.issubset(set(context.allowed_companies))
    )


def staff_access_permission_query(user: str | None = None) -> str:
    resolved_user = _current_user(user)
    roles = _roles(resolved_user)
    system_manager = SYSTEM_MANAGER_ROLE in roles
    if system_manager:
        return ""
    if DIRECTOR_ROLE not in roles:
        return "1=0"
    companies = allowed_companies(resolved_user)
    if not companies:
        return "1=0"
    escaped = ", ".join(frappe.db.escape(company) for company in companies)
    return (
        "EXISTS (SELECT 1 FROM `tabKoPOS Staff Outlet` scoped_outlet "
        "WHERE scoped_outlet.parent = `tabKoPOS Staff Access`.name "
        "AND scoped_outlet.parenttype = 'KoPOS Staff Access' "
        f"AND scoped_outlet.company IN ({escaped})) "
        "AND NOT EXISTS (SELECT 1 FROM `tabKoPOS Staff Outlet` outside_outlet "
        "WHERE outside_outlet.parent = `tabKoPOS Staff Access`.name "
        "AND outside_outlet.parenttype = 'KoPOS Staff Access' "
        f"AND outside_outlet.company NOT IN ({escaped}))"
    )


def has_staff_access_permission(
    document: Any,
    user: str | None = None,
    permission_type: str | None = None,
) -> bool:
    del permission_type
    resolved_user = _current_user(user)
    roles = _roles(resolved_user)
    system_manager = SYSTEM_MANAGER_ROLE in roles
    if system_manager:
        return True
    if DIRECTOR_ROLE not in roles:
        return False
    access = cstr(getattr(document, "name", None)).strip()
    if access:
        return can_manage_staff_access(access, user=resolved_user)
    assignments = list(getattr(document, "outlet_assignments", None) or ())
    companies = {
        cstr(getattr(row, "company", None)).strip()
        for row in assignments
        if cstr(getattr(row, "company", None)).strip()
    }
    return bool(companies) and companies.issubset(set(allowed_companies(resolved_user)))


@frappe.whitelist(methods=["GET"])
def get_director_outlet(
    company: str | None = None,
    warehouse: str | None = None,
) -> dict[str, Any]:
    context = resolve_director_context(company=company, warehouse=warehouse)
    rows = (
        frappe.get_all(
            "Warehouse",
            filters={"name": ["in", list(context.allowed_warehouses)]},
            fields=["name", "warehouse_name"],
            order_by="warehouse_name asc, name asc",
            limit_page_length=CONFIGURATION_LIMIT,
        )
        if context.allowed_warehouses
        else []
    )
    return {
        "status": context.status,
        "company": context.company,
        "warehouse": context.warehouse,
        "companies": list(context.allowed_companies),
        "warehouses": list(context.allowed_warehouses),
        "outlets": [
            {
                "value": cstr(row.get("name")).strip(),
                "label": cstr(row.get("warehouse_name")).strip()
                or cstr(row.get("name")).strip(),
            }
            for row in rows
            if cstr(row.get("name")).strip()
        ],
    }
