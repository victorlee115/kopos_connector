from __future__ import annotations

from types import SimpleNamespace

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api import director_scope  # noqa: E402


def _install_site(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: str = "director@example.test",
    roles: tuple[str, ...] = ("Company Director",),
    company_permissions: tuple[str, ...] = ("Moonleaf Cafe",),
    warehouse_permissions: tuple[str, ...] = (),
    companies: tuple[str, ...] = ("Moonleaf Cafe", "Other Cafe"),
    defaults: dict[str, object] | None = None,
) -> None:
    profiles = {
        "Moonleaf Cafe": [
            {
                "name": "Moonleaf Counter",
                "warehouse": "Moonleaf Front - MC",
                "selling_price_list": "Moonleaf Menu",
            },
            {
                "name": "Moonleaf Kiosk",
                "warehouse": "Moonleaf Kiosk - MC",
                "selling_price_list": "Moonleaf Kiosk Menu",
            },
        ],
        "Other Cafe": [
            {
                "name": "Other Counter",
                "warehouse": "Other Front - OC",
                "selling_price_list": "Other Menu",
            }
        ],
    }

    def get_all(doctype: str, *, filters=None, pluck=None, **_kwargs):
        if doctype == "Company":
            return list(companies)
        if doctype == "User Permission":
            allow = (filters or {}).get("allow")
            values = company_permissions if allow == "Company" else warehouse_permissions
            return list(values)
        if doctype == "POS Profile":
            if (filters or {}).get("warehouse"):
                wanted = (filters or {}).get("warehouse")
                return [
                    {"company": company}
                    for company, rows in profiles.items()
                    if any(row.get("warehouse") == wanted for row in rows)
                ]
            return list(profiles.get((filters or {}).get("company"), ()))
        if doctype == "KoPOS Staff Outlet":
            return []
        raise AssertionError(f"Unexpected read of {doctype}")

    monkeypatch.setattr(director_scope.frappe.session, "user", user)
    monkeypatch.setattr(director_scope.frappe, "get_roles", lambda selected: list(roles) if selected == user else [])
    monkeypatch.setattr(director_scope.frappe, "get_all", get_all)
    monkeypatch.setattr(
        director_scope.frappe,
        "defaults",
        SimpleNamespace(get_defaults=lambda: dict(defaults or {})),
        raising=False,
    )
    monkeypatch.setattr(
        director_scope.frappe,
        "db",
        SimpleNamespace(
            get_single_value=lambda *_args, **_kwargs: "Standard Selling",
            escape=lambda value: f"'{value}'",
        ),
    )


def test_director_without_explicit_company_permission_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, company_permissions=())

    context = director_scope.resolve_director_context()

    assert context.status == "permission_denied"
    assert context.company == ""
    assert context.allowed_companies == ()


def test_only_permitted_company_is_selected_not_only_site_company(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(
        monkeypatch,
        company_permissions=("Moonleaf Cafe",),
        companies=("Moonleaf Cafe", "Other Cafe"),
    )

    context = director_scope.resolve_director_context(warehouse="Moonleaf Front - MC")

    assert context.company == "Moonleaf Cafe"
    assert context.warehouse == "Moonleaf Front - MC"
    assert context.allowed_price_lists == ("Moonleaf Menu",)


def test_default_never_grants_access(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(
        monkeypatch,
        company_permissions=("Moonleaf Cafe",),
        defaults={"Company": "Other Cafe", "Warehouse": "Other Front - OC"},
    )

    context = director_scope.resolve_director_context()

    assert context.company == "Moonleaf Cafe"
    assert context.warehouse == ""
    assert context.status == "selection_required"


def test_requested_company_outside_scope_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch)

    with pytest.raises(director_scope.frappe.PermissionError):
        director_scope.resolve_director_context(company="Other Cafe")


def test_warehouse_permissions_intersect_company_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, warehouse_permissions=("Moonleaf Kiosk - MC", "Other Front - OC"))

    context = director_scope.resolve_director_context()

    assert context.allowed_warehouses == ("Moonleaf Kiosk - MC",)
    assert context.warehouse == "Moonleaf Kiosk - MC"
    assert context.allowed_price_lists == ("Moonleaf Kiosk Menu",)


def test_requested_warehouse_outside_scope_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, warehouse_permissions=("Moonleaf Front - MC",))

    with pytest.raises(director_scope.frappe.PermissionError):
        director_scope.resolve_director_context(warehouse="Moonleaf Kiosk - MC")


def test_permitted_warehouse_can_choose_its_company_without_a_second_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(
        monkeypatch,
        company_permissions=("Moonleaf Cafe", "Other Cafe"),
        warehouse_permissions=("Other Front - OC",),
    )

    context = director_scope.resolve_director_context(warehouse="Other Front - OC")

    assert context.company == "Other Cafe"
    assert context.warehouse == "Other Front - OC"


def test_system_manager_can_choose_any_company(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, roles=("System Manager",), company_permissions=())

    context = director_scope.resolve_director_context(
        company="Other Cafe",
        warehouse="Other Front - OC",
    )

    assert context.company == "Other Cafe"
    assert context.warehouse == "Other Front - OC"


def test_business_director_lands_on_the_plain_jiji_home(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch)

    assert director_scope.business_home_page("director@example.test") == (
        "desk/jiji_home?sidebar=JiJi"
    )


def test_support_account_keeps_the_standard_desk_home(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, roles=("Company Director", "System Manager"))

    assert director_scope.business_home_page("director@example.test") is None


def test_permission_hook_denies_non_director_without_throwing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, roles=("Accounts User",))

    assert director_scope.staff_access_permission_query() == "1=0"
    assert director_scope.has_staff_access_permission(SimpleNamespace(name="staff@example.test")) is False


def test_permission_query_requires_every_assignment_to_stay_in_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_site(monkeypatch, company_permissions=("Moonleaf Cafe",))

    condition = director_scope.staff_access_permission_query()

    assert "EXISTS" in condition
    assert "NOT EXISTS" in condition
    assert "'Moonleaf Cafe'" in condition
