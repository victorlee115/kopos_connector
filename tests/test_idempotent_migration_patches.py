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
    sql_calls: list[str] = []
    commit_calls: list[None] = []

    monkeypatch.setattr(add_order_reference.frappe.db, "exists", lambda *_args: True)
    monkeypatch.setattr(
        add_order_reference.frappe,
        "reload_doc",
        lambda *_args: None,
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
