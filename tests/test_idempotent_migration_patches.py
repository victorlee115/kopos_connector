from __future__ import annotations

from .fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()


from kopos_connector.patches import (
    add_order_reference_to_fb_stock_override_log as add_order_reference,
)
from kopos_connector.patches import (
    quarantine_legacy_modifier_report as quarantine_legacy_report,
)
from kopos_connector.patches import (
    remove_duplicate_modifier_client_script as remove_duplicate_script,
)


def test_order_reference_schema_patch_is_safe_to_replay(monkeypatch) -> None:
    column_exists = False
    reload_calls: list[tuple[str, str, str]] = []
    sql_calls: list[str] = []
    commit_calls: list[None] = []

    monkeypatch.setattr(add_order_reference.frappe.db, "exists", lambda *_args: True)
    monkeypatch.setattr(
        add_order_reference.frappe,
        "reload_doc",
        lambda *args: reload_calls.append(args),
        raising=False,
    )
    monkeypatch.setattr(
        add_order_reference.frappe.db,
        "has_column",
        lambda *_args: column_exists,
    )

    def execute_sql(statement: str) -> None:
        nonlocal column_exists
        sql_calls.append(statement)
        column_exists = True

    monkeypatch.setattr(add_order_reference.frappe.db, "sql", execute_sql)
    monkeypatch.setattr(
        add_order_reference.frappe.db,
        "commit",
        lambda: commit_calls.append(None),
    )

    add_order_reference.execute()
    add_order_reference.execute()

    assert reload_calls == [
        ("kopos", "doctype", "fb_stock_override_log"),
        ("kopos", "doctype", "fb_stock_override_log"),
    ]
    assert len(sql_calls) == 1
    assert "ADD COLUMN `order_reference`" in sql_calls[0]
    assert len(commit_calls) == 1


def test_duplicate_client_script_patch_is_safe_to_replay(monkeypatch) -> None:
    script_exists = True
    deleted: list[tuple[str, str]] = []
    commits: list[None] = []

    monkeypatch.setattr(
        remove_duplicate_script.frappe.db,
        "exists",
        lambda *_args: script_exists,
    )

    def delete_doc(doctype: str, name: str, *, ignore_permissions: bool) -> None:
        nonlocal script_exists
        assert ignore_permissions is True
        deleted.append((doctype, name))
        script_exists = False

    monkeypatch.setattr(
        remove_duplicate_script.frappe,
        "delete_doc",
        delete_doc,
        raising=False,
    )
    monkeypatch.setattr(
        remove_duplicate_script.frappe.db,
        "commit",
        lambda: commits.append(None),
    )

    remove_duplicate_script.execute()
    remove_duplicate_script.execute()

    assert deleted == [("Client Script", "KoPOS POS Invoice Modifier Display")]
    assert len(commits) == 1


def test_legacy_report_quarantine_patch_is_safe_to_replay(monkeypatch) -> None:
    deleted: list[tuple[str, dict[str, str]]] = []
    disabled: list[tuple[str, str, str, int]] = []

    monkeypatch.setattr(
        quarantine_legacy_report.frappe.db,
        "delete",
        lambda doctype, filters: deleted.append((doctype, filters)),
    )
    monkeypatch.setattr(
        quarantine_legacy_report.frappe.db,
        "exists",
        lambda *_args: True,
    )

    def set_value(
        doctype: str,
        name: str,
        fieldname: str,
        value: int,
        *,
        update_modified: bool,
    ) -> None:
        assert update_modified is False
        disabled.append((doctype, name, fieldname, value))

    monkeypatch.setattr(quarantine_legacy_report.frappe.db, "set_value", set_value)

    quarantine_legacy_report.execute()
    quarantine_legacy_report.execute()

    assert deleted == [
        (
            "Scheduled Job Type",
            {"method": quarantine_legacy_report.LEGACY_SCHEDULED_METHOD},
        ),
        (
            "Scheduled Job Type",
            {"method": quarantine_legacy_report.LEGACY_SCHEDULED_METHOD},
        ),
    ]
    assert disabled == [
        (
            "Report",
            quarantine_legacy_report.LEGACY_REPORT_NAME,
            "disabled",
            1,
        ),
        (
            "Report",
            quarantine_legacy_report.LEGACY_REPORT_NAME,
            "disabled",
            1,
        ),
    ]


def test_branch_qr_migration_creates_profile_fields_before_migrating_store_qr(monkeypatch) -> None:
    """Upgrade from 1.0.11: POS Profile QR fields do not exist until after_migrate."""
    import sys
    from types import ModuleType, SimpleNamespace

    from kopos_connector.patches import migrate_branch_scoped_maybank_qr as qr_patch

    events: list[str] = []
    fields_created = False
    installer = ModuleType("kopos_connector.install.install")

    def create_kopos_custom_fields() -> None:
        nonlocal fields_created
        events.append("create_fields")
        fields_created = True

    installer.create_kopos_custom_fields = create_kopos_custom_fields
    monkeypatch.setitem(sys.modules, "kopos_connector.install.install", installer)

    class Profile(SimpleNamespace):
        def save(self, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            # Native Document.save() drops attributes whose fields are unknown.
            assert fields_created, "profile saved before its QR fields existed"
            events.append(f"save:{self.name}")

    profile = Profile(name="Booth", custom_kopos_static_qr_payload="", custom_kopos_maybank_qrpaybiz_account="")
    device = SimpleNamespace(
        static_qr_payload="PAYLOAD", static_qr_payload_sha256="sha", static_qr_merchant_id="123",
        static_qr_acquirer_id="501664", static_qr_merchant_name="STORE", static_qr_version="02",
        static_qr_commissioned_at="2026-08-01 10:00:00",
    )

    def get_doc(doctype: str, name: str):
        events.append(f"get_doc:{doctype}")
        return profile if doctype == "POS Profile" else device

    monkeypatch.setattr(qr_patch.frappe, "reload_doc", lambda *args: events.append("reload"), raising=False)
    monkeypatch.setattr(qr_patch.frappe.db, "exists", lambda doctype, name=None: doctype == "DocType")
    monkeypatch.setattr(qr_patch.frappe.db, "table_exists", lambda doctype: True, raising=False)
    monkeypatch.setattr(qr_patch.frappe, "clear_cache", lambda **kwargs: events.append(f"clear_cache:{kwargs.get('doctype')}"), raising=False)
    monkeypatch.setattr(qr_patch.frappe, "get_single", lambda doctype: SimpleNamespace(username="", outlet_id="OUTLET"), raising=False)
    monkeypatch.setattr(qr_patch.frappe, "get_all", lambda doctype, **kwargs: [{"pos_profile": "Booth", "device_id": "TAB-1"}] if doctype == "KoPOS Device" else [], raising=False)
    monkeypatch.setattr(qr_patch.frappe, "get_doc", get_doc, raising=False)

    qr_patch.execute()

    assert events.index("create_fields") < events.index("clear_cache:POS Profile") < events.index("get_doc:POS Profile")
    # No legacy Maybank login: no automatic-QR account, but the store QR still migrates.
    assert profile.custom_kopos_maybank_qrpaybiz_account == ""
    assert profile.custom_kopos_static_qr_enabled == 1
    assert profile.custom_kopos_static_qr_payload == "PAYLOAD"
    assert profile.custom_kopos_static_qr_merchant_id == "123"
    assert events.count("save:Booth") == 1
