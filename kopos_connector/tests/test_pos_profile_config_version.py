from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

import frappe

from kopos_connector import hooks, smoke
from kopos_connector.api import catalog, devices
from kopos_connector.extensions import pos_profile


BASE_PROFILE_CONFIG = {
    "company": "KoPOS Malaysia Sdn Bhd",
    "warehouse": "KoPOS Store - KMY",
    "currency": "MYR",
    "custom_kopos_enable_sst": 1,
    "custom_kopos_sst_rate": 8.0,
}


def test_pos_profile_extension_is_registered_without_doc_event_lifecycle() -> None:
    assert hooks.extend_doctype_class == {
        "Journal Entry": [
            "kopos_connector.extensions.journal_entry.KoPOSJournalEntryIntegrityMixin"
        ],
        "POS Profile": [
            "kopos_connector.extensions.pos_profile.KoPOSPOSProfileConfigMixin"
        ],
        "Sales Invoice": [
            "kopos_connector.extensions.sales_invoice.KoPOSSalesInvoiceIntegrityMixin"
        ],
    }
    assert not hasattr(hooks, "doc_events")
    assert pos_profile.SERIALIZED_POS_PROFILE_FIELDS == (
        "company",
        "warehouse",
        "currency",
        "custom_kopos_enable_sst",
        "custom_kopos_sst_rate",
    )


def test_profile_rename_invalidates_devices_after_parent_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class ParentProfile:
        def after_rename(
            self,
            old_name: str,
            new_name: str,
            merge: bool = False,
        ) -> None:
            calls.append(("parent", old_name, new_name, merge))

    class ExtendedProfile(pos_profile.KoPOSPOSProfileConfigMixin, ParentProfile):
        pass

    monkeypatch.setattr(
        pos_profile,
        "bump_bound_device_config_versions",
        lambda profile_name: calls.append(("bump", profile_name)),
    )

    ExtendedProfile().after_rename("KoPOS Old", "KoPOS Main", merge=False)

    assert calls == [
        ("parent", "KoPOS Old", "KoPOS Main", False),
        ("bump", "KoPOS Main"),
    ]


@pytest.mark.parametrize(
    ("fieldname", "next_value"),
    (
        ("company", "Another Company"),
        ("warehouse", "Another Warehouse - KMY"),
        ("currency", "SGD"),
        ("custom_kopos_enable_sst", 0),
        ("custom_kopos_sst_rate", 6.0),
    ),
)
def test_each_serialized_profile_change_atomically_bumps_all_bound_devices(
    monkeypatch: pytest.MonkeyPatch,
    fieldname: str,
    next_value: Any,
) -> None:
    sql_calls: list[tuple[str, dict[str, Any]]] = []
    before = dict(BASE_PROFILE_CONFIG)
    after = {**before, fieldname: next_value}
    profile = SimpleNamespace(
        name="KoPOS Main",
        **after,
        get_doc_before_save=lambda: before,
    )
    monkeypatch.setattr(
        frappe.db,
        "sql",
        lambda query, values: sql_calls.append((query, values)),
    )

    changed = pos_profile.invalidate_bound_device_configs_for_profile(profile)

    assert changed is True
    assert len(sql_calls) == 1
    query, values = sql_calls[0]
    assert "GREATEST(COALESCE(`config_version`, 0), 1) + 1" in query
    assert "WHERE `pos_profile` = %(pos_profile)s" in query
    assert "LIMIT" not in query.upper()
    assert values["pos_profile"] == "KoPOS Main"


def test_unrelated_profile_save_does_not_churn_device_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = {**BASE_PROFILE_CONFIG, "customer": "Walk-in Customer"}
    profile = SimpleNamespace(
        name="KoPOS Main",
        **BASE_PROFILE_CONFIG,
        customer="Another Customer",
        get_doc_before_save=lambda: before,
    )
    monkeypatch.setattr(
        frappe.db,
        "sql",
        lambda *args, **kwargs: pytest.fail("unrelated save must not bump devices"),
    )

    assert pos_profile.invalidate_bound_device_configs_for_profile(profile) is False


def test_new_profile_does_not_bump_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(
        name="KoPOS Main",
        **BASE_PROFILE_CONFIG,
        get_doc_before_save=lambda: None,
    )
    monkeypatch.setattr(
        frappe.db,
        "sql",
        lambda *args, **kwargs: pytest.fail("new profile cannot have bound devices"),
    )

    assert pos_profile.invalidate_bound_device_configs_for_profile(profile) is False


def test_existing_smoke_profile_is_repaired_to_explicitly_disable_sst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExistingProfile:
        name = "KoPOS Main"
        company = "Old Company"
        currency = "USD"
        selling_price_list = "Old Selling"
        warehouse = "Old Warehouse"
        customer = "Old Customer"
        write_off_account = "Old Write Off"
        write_off_cost_center = "Old Cost Center"
        write_off_limit = 10
        custom_kopos_enable_sst = 1
        custom_kopos_sst_rate = 8.0

        def __init__(self) -> None:
            self.payments = [
                SimpleNamespace(mode_of_payment="Cash"),
                SimpleNamespace(mode_of_payment="DuitNow QR"),
            ]
            self.saved = False

        def append(self, fieldname: str, row: dict[str, Any]) -> None:
            getattr(self, fieldname).append(SimpleNamespace(**row))

        def save(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            self.saved = True

    profile = ExistingProfile()
    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, name: "KoPOS Main" if doctype == "POS Profile" else False,
    )
    monkeypatch.setattr(frappe, "get_doc", lambda *args, **kwargs: profile)
    monkeypatch.setattr(smoke, "_get_demo_currency", lambda company: "MYR")
    monkeypatch.setattr(
        smoke,
        "_ensure_selling_price_list",
        lambda currency: smoke.SMOKE_SELLING_PRICE_LIST,
    )

    result = smoke._ensure_pos_profile(
        company=smoke.SMOKE_COMPANY_NAME,
        warehouse="KoPOS Store - KMY",
        customer="Walk-in Customer",
        write_off_account="Write Off - KMY",
        write_off_cost_center="Main - KMY",
    )

    assert result == "KoPOS Main"
    assert profile.custom_kopos_enable_sst == smoke.SMOKE_ENABLE_SST == 0
    assert profile.custom_kopos_sst_rate == smoke.SMOKE_SST_RATE_PERCENT == 8
    assert profile.selling_price_list == smoke.SMOKE_SELLING_PRICE_LIST
    assert profile.saved is True


def test_empty_terminal_tax_repair_changes_only_profile_and_invalidates_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Device:
        name = "KOPOS-DEVICE-001"
        device_id = smoke.SMOKE_DEVICE_ID
        pos_profile = "KoPOS Main"
        config_version = 10

        def reload(self) -> None:
            return None

    class Profile:
        custom_kopos_enable_sst = 1
        custom_kopos_sst_rate = 8.0

        def __init__(self) -> None:
            self.saved = False

        def save(self, *, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            self.saved = True
            device.config_version += 1

    device = Device()
    profile = Profile()
    commits: list[str] = []
    monkeypatch.setattr(smoke, "_require_empty_smoke_terminal", lambda operation: {})
    monkeypatch.setattr(
        frappe.db,
        "exists",
        lambda doctype, filters: device.name
        if (doctype, filters) == (
            "KoPOS Device",
            {"device_id": smoke.SMOKE_DEVICE_ID},
        )
        else False,
    )
    monkeypatch.setattr(
        frappe,
        "get_doc",
        lambda doctype, name: device if doctype == "KoPOS Device" else profile,
    )
    monkeypatch.setattr(frappe.db, "commit", lambda: commits.append("commit"))
    monkeypatch.setattr(
        devices,
        "serialize_device_config",
        lambda doc: {
            "config_version": doc.config_version,
            "tax_rate": (
                0.0 if profile.custom_kopos_enable_sst == 0 else 0.08
            ),
        },
    )

    result = smoke.repair_empty_smoke_tax_config_json()

    assert profile.saved is True
    assert profile.custom_kopos_enable_sst == 0
    assert result["effective_tax_rate"] == 0.0
    assert result["config_version_before"] == 10
    assert result["config_version_after"] == 11
    assert result["business_state_preserved_empty"] is True
    assert commits == ["commit"]


def test_empty_terminal_guard_rejects_existing_business_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        smoke,
        "dump_smoke_state",
        lambda: {
            "status": "ready",
            "data": {
                "fb_orders": [{"name": "FB-ORDER-EXISTING"}],
                "projection_statuses": {"rows": []},
            },
        },
    )

    with pytest.raises(
        frappe.ValidationError,
        match="requires empty terminal business state",
    ):
        smoke._require_empty_smoke_terminal("Smoke tax config repair")


@pytest.mark.parametrize(
    ("enabled", "rate_percent", "expected"),
    ((1, 6.0, 0.06), (1, 8.0, 0.08), (0, 8.0, 0.0)),
)
def test_general_profiles_retain_configurable_sst_behavior(
    monkeypatch: pytest.MonkeyPatch,
    enabled: int,
    rate_percent: float,
    expected: float,
) -> None:
    values = {
        "custom_kopos_enable_sst": enabled,
        "custom_kopos_sst_rate": rate_percent,
    }
    profile = SimpleNamespace(as_dict=lambda: values)
    monkeypatch.setattr(frappe, "get_doc", lambda *args, **kwargs: profile)

    assert catalog.get_tax_rate_value(pos_profile_name="Production Profile") == expected


def test_smoke_dump_evidence_exposes_profile_and_effective_tax(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = SimpleNamespace(
        company=smoke.SMOKE_COMPANY_NAME,
        customer="Walk-in Customer",
        warehouse="KoPOS Store - KMY",
        currency="MYR",
        custom_kopos_enable_sst=0,
        custom_kopos_sst_rate=8.0,
    )
    monkeypatch.setattr(frappe, "get_cached_doc", lambda *args, **kwargs: profile)
    monkeypatch.setattr(catalog, "get_tax_rate_value", lambda **kwargs: 0.0)

    result = smoke._collect_device_profile_evidence(
        SimpleNamespace(pos_profile="KoPOS Main")
    )

    assert result["pos_profile_sst_enabled"] is False
    assert result["pos_profile_sst_rate_percent"] == 8.0
    assert result["device_config_tax_rate"] == 0.0
