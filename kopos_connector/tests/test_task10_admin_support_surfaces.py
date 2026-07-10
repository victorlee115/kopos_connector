from __future__ import annotations

import json
from pathlib import Path


CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROLES = {"System Manager", "KoPOS Manager", "POS Manager"}
CASHIER_ROLES = {"KoPOS Cashier", "POS User", "All"}
FORBIDDEN_SECRET_TERMS = {
    "api_key",
    "api_secret",
    "pin_hash",
    "raw_response",
    "qr_data",
    "password",
    "bearer",
    "token",
}
FORBIDDEN_ACTIVE_LEGACY_TERMS = {
    "POS Invoice",
    "POS Opening Entry",
    "POS Closing Entry",
    "pos_invoice",
    "pos_opening_entry",
    "pos_closing_entry",
}


def load_json(relative_path: str) -> dict[str, object]:
    return json.loads((CONNECTOR_ROOT / relative_path).read_text())


def roles_from_rows(rows: object) -> set[str]:
    assert isinstance(rows, list)
    roles: set[str] = set()
    for row in rows:
        assert isinstance(row, dict)
        role = row.get("role")
        if isinstance(role, str):
            roles.add(role)
    return roles


def test_workspace_is_manager_support_only_and_uses_canonical_links() -> None:
    workspace = load_json("kopos/workspace/kopos_support/kopos_support.json")
    roles = roles_from_rows(workspace["roles"])
    assert ADMIN_ROLES.issubset(roles)
    assert CASHIER_ROLES.isdisjoint(roles)

    serialized = json.dumps(workspace, sort_keys=True)
    for expected in (
        "KoPOS Device",
        "FB Shift",
        "FB Order",
        "Sales Invoice",
        "FB Projection Log",
        "projection_status",
        "KoPOS Device Health",
        "KoPOS Projection Support Queue",
    ):
        assert expected in serialized
    for forbidden in FORBIDDEN_ACTIVE_LEGACY_TERMS:
        assert forbidden not in serialized


def test_provisioning_page_and_buttons_stay_system_manager_only() -> None:
    page = load_json("page/kopos_provisioning/kopos_provisioning.json")
    assert roles_from_rows(page["roles"]) == {"System Manager"}

    provisioning_api = (CONNECTOR_ROOT / "api/provisioning.py").read_text()
    assert "def create_device_provisioning_qr" in provisioning_api
    assert "require_system_manager()" in provisioning_api

    install_source = (CONNECTOR_ROOT / "install/install.py").read_text()
    assert '(frappe.user_roles || []).includes("System Manager")' in install_source
    assert "The one-time setup link is hidden after creation" in install_source
    assert "<code>${koposEscapeHtml(payload.provisioning_link)}</code>" not in install_source


def test_support_reports_exclude_cashier_roles_and_secret_fields() -> None:
    report_paths = [
        "kopos/report/kopos_device_health/kopos_device_health.json",
        "kopos/report/kopos_projection_support_queue/kopos_projection_support_queue.json",
    ]
    source_paths = [
        "kopos/report/kopos_device_health/kopos_device_health.py",
        "kopos/report/kopos_projection_support_queue/kopos_projection_support_queue.py",
    ]

    for relative_path in report_paths:
        report = load_json(relative_path)
        roles = roles_from_rows(report["roles"])
        assert ADMIN_ROLES.issubset(roles)
        assert CASHIER_ROLES.isdisjoint(roles)
        assert report["report_type"] == "Script Report"

    for relative_path in source_paths:
        source = (CONNECTOR_ROOT / relative_path).read_text().lower()
        for forbidden in FORBIDDEN_SECRET_TERMS:
            assert forbidden not in source


def test_device_health_report_has_required_support_columns() -> None:
    source = (CONNECTOR_ROOT / "kopos/report/kopos_device_health/kopos_device_health.py").read_text()
    for expected in (
        "last_seen_at",
        "pos_profile",
        "config_version",
        "enabled",
        "printer_summary",
        "user_summary",
        "provisioning_action",
    ):
        assert expected in source


def test_projection_queue_uses_canonical_support_review_vocabulary() -> None:
    source = (CONNECTOR_ROOT / "kopos/report/kopos_projection_support_queue/kopos_projection_support_queue.py").read_text()
    for expected in (
        "projection_status",
        "Support review required",
        "FB Projection Log",
        "idempotency_key",
    ):
        assert expected in source
    for forbidden in FORBIDDEN_ACTIVE_LEGACY_TERMS:
        assert forbidden not in source
