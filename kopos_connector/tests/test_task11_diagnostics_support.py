# pyright: reportMissingImports=false

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


CONNECTOR_ROOT = Path(__file__).resolve().parents[1]
ADMIN_ROLES = {"System Manager", "KoPOS Manager", "POS Manager"}
CASHIER_ROLES = {"KoPOS Cashier", "POS User", "All"}
FORBIDDEN_SECRET_TERMS = {
    "api-key-secret",
    "api-secret-value",
    "bearer abc",
    "pin-hash-value",
    "raw-qr-payload",
    "setup-token",
}
FORBIDDEN_ACTIVE_LEGACY_TERMS = {
    "POS Invoice",
    "POS Opening Entry",
    "POS Closing Entry",
    "pos_invoice",
    "pos_opening_entry",
    "pos_closing_entry",
}
FORBIDDEN_DESTRUCTIVE_ACTION_TERMS = {
    "force_green",
    "force success",
    "clear queue",
    "erase erp",
    "delete row",
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


def test_task11_reports_are_manager_support_only_and_linked_from_workspace() -> None:
    report_paths = [
        "kopos/report/kopos_shift_reconciliation/kopos_shift_reconciliation.json",
        "kopos/report/kopos_support_report/kopos_support_report.json",
        "kopos/report/kopos_projection_support_queue/kopos_projection_support_queue.json",
    ]
    for relative_path in report_paths:
        report = load_json(relative_path)
        roles = roles_from_rows(report["roles"])
        assert ADMIN_ROLES.issubset(roles)
        assert CASHIER_ROLES.isdisjoint(roles)
        assert report["report_type"] == "Script Report"

    workspace = load_json("kopos/workspace/kopos_support/kopos_support.json")
    serialized = json.dumps(workspace, sort_keys=True)
    assert "KoPOS Shift Reconciliation" in serialized
    assert "KoPOS Support Report" in serialized


def test_projection_queue_exposes_required_diagnostics_without_unsafe_actions() -> None:
    source = (
        CONNECTOR_ROOT
        / "kopos/report/kopos_projection_support_queue/kopos_projection_support_queue.py"
    ).read_text()
    for expected in (
        "affected_order",
        "failure_reason",
        "idempotency_key",
        "next_action",
        "safe_retry",
        "retry_count",
        "projection_status",
    ):
        assert expected in source
    lowered = source.lower()
    for forbidden in FORBIDDEN_DESTRUCTIVE_ACTION_TERMS:
        assert forbidden not in lowered


def test_shift_reconciliation_links_shift_orders_invoices_payments_and_projection_state() -> None:
    source = (
        CONNECTOR_ROOT
        / "kopos/report/kopos_shift_reconciliation/kopos_shift_reconciliation.py"
    ).read_text()
    for expected in (
        "FB Shift",
        "FB Order",
        "Sales Invoice",
        "payments",
        "refund_void_state",
        "projection_status",
        "cash_variance",
        "idempotency_key",
    ):
        assert expected in source
    lowered = source.lower()
    for forbidden in FORBIDDEN_DESTRUCTIVE_ACTION_TERMS:
        assert forbidden not in lowered


def test_shift_reconciliation_get_rows_does_not_hide_query_failures() -> None:
    source = (
        CONNECTOR_ROOT
        / "kopos/report/kopos_shift_reconciliation/kopos_shift_reconciliation.py"
    ).read_text()
    get_rows_source = source.split("def _get_rows(", 1)[1]
    get_rows_source = get_rows_source.split("def _money(", 1)[0]

    assert "frappe.get_all" in get_rows_source
    assert "except Exception" not in get_rows_source
    assert "return []" not in get_rows_source


def test_smoke_support_get_rows_does_not_hide_query_failures(monkeypatch) -> None:
    install_fake_frappe_modules()

    import frappe

    from kopos_connector import smoke

    source = (CONNECTOR_ROOT / "smoke.py").read_text()
    get_rows_source = source.split("def _get_rows(", 1)[1]
    get_rows_source = get_rows_source.split("def _money(", 1)[0]

    assert "frappe.get_all" in get_rows_source
    assert "except Exception" not in get_rows_source
    assert "return []" not in get_rows_source

    def raise_query_failure(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("support query failed")

    monkeypatch.setattr(frappe, "get_all", raise_query_failure, raising=False)

    with pytest.raises(RuntimeError, match="support query failed"):
        smoke._get_rows("FB Projection Log")


def test_smoke_support_payload_proves_idempotency_projection_and_legacy_status() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    state = build_sample_smoke_state()
    payload = smoke.build_smoke_support_report(
        state,
        expected_idempotency_keys=["idem-001"],
    )

    assert payload["status"] == "support_ready"
    proof = payload["proof"]
    assert proof["idempotency_status"] == "clear"
    assert proof["one_sales_invoice_per_expected_key"] == {"idem-001": True}
    assert proof["active_legacy_path_count"] == 0
    assert proof["legacy_path_status"] == "clear"
    assert proof["projection_status"] == "clear"
    assert payload["projection_status"]["counts_by_state"] == {"Succeeded": 1}


def test_smoke_support_payload_redacts_sensitive_fields_and_omits_legacy_terms() -> None:
    install_fake_frappe_modules()

    from kopos_connector import smoke

    state = build_sample_smoke_state()
    device = state["device"]
    assert isinstance(device, dict)
    device["api_key"] = "api-key-secret"
    state["api_key"] = "api-key-secret"
    data = state["data"]
    assert isinstance(data, dict)
    data["api_secret"] = "api-secret-value"
    data["pin_hash"] = "pin-hash-value"
    data["qr_data"] = "raw-qr-payload"
    data["provisioning_token"] = "setup-token"
    data["bearer_header"] = "Bearer abc"

    payload = smoke.build_smoke_support_report(state)
    serialized = json.dumps(payload, sort_keys=True)

    for forbidden in FORBIDDEN_SECRET_TERMS:
        assert forbidden not in serialized
    for forbidden in FORBIDDEN_ACTIVE_LEGACY_TERMS:
        assert forbidden not in serialized
    assert "[redacted]" in serialized


def test_support_report_rows_are_static_support_summary_rows() -> None:
    install_fake_frappe_modules()

    from kopos_connector.kopos.report.kopos_support_report import kopos_support_report

    rows = kopos_support_report._rows_from_payload(
        {
            "status": "support_ready",
            "summary": {"fb_shifts": 1, "fb_orders": 1, "sales_invoices": 1},
            "proof": {
                "idempotency_status": "clear",
                "duplicate_sales_invoice_keys": [],
                "expected_idempotency_keys": ["idem-001"],
                "legacy_path_status": "clear",
                "active_legacy_path_count": 0,
                "projection_status": "clear",
                "failed_projection_count": 0,
            },
            "projection_status": {"counts_by_state": {"Succeeded": 1}},
            "reconciliation": {
                "status": "clear",
                "payment_rows": 1,
                "return_records": 1,
                "void_records": 1,
                "cash_variance_rows": 1,
            },
            "next_action": "Support report is clear; archive as smoke evidence",
        }
    )

    sections = {row["section"] for row in rows}
    assert "Idempotency" in sections
    assert "Legacy Path Status" in sections
    assert "Projection Status" in sections
    assert "Reconciliation" in sections


def build_sample_smoke_state() -> dict[str, object]:
    return {
        "status": "ready",
        "site": "test-site",
        "device": {"device_id": "SMOKE-TAB-A001", "enabled": True},
        "data": {
            "fb_shifts": [
                {
                    "name": "SHIFT-001",
                    "shift_code": "smoke-shift-001",
                    "status": "Closed",
                    "opening_float": 0.0,
                    "expected_cash": 0.0,
                    "counted_cash": 0.0,
                    "cash_variance": 0.0,
                }
            ],
            "fb_orders": [
                {
                    "name": "ORDER-001",
                    "order_id": "smoke-order-001",
                    "external_idempotency_key": "idem-001",
                    "shift": "SHIFT-001",
                    "status": "Submitted",
                    "invoice_status": "Posted",
                    "stock_status": "Posted",
                    "sales_invoice": "SI-001",
                    "grand_total": 12.0,
                    "currency": "MYR",
                }
            ],
            "sales_invoices": [
                {
                    "name": "SI-001",
                    "docstatus": 1,
                    "status": "Paid",
                    "is_return": False,
                    "grand_total": 12.0,
                    "paid_amount": 12.0,
                    "outstanding_amount": 0.0,
                    "custom_fb_order": "ORDER-001",
                    "custom_fb_shift": "SHIFT-001",
                    "custom_fb_idempotency_key": "idem-001",
                    "items": [{"item_code": "ITEM-COFFEE"}],
                    "payments": [{"mode_of_payment": "Cash", "amount": 12.0}],
                },
                {
                    "name": "SI-RET-001",
                    "docstatus": 1,
                    "status": "Return",
                    "is_return": True,
                    "return_against": "SI-001",
                    "grand_total": -12.0,
                    "paid_amount": 0.0,
                    "outstanding_amount": 0.0,
                    "custom_fb_order": "ORDER-001",
                    "custom_fb_shift": "SHIFT-001",
                    "custom_fb_idempotency_key": "idem-001:return",
                    "items": [{"item_code": "ITEM-COFFEE"}],
                    "payments": [],
                }
            ],
            "sales_invoice_payments": [
                {"sales_invoice": "SI-001", "mode_of_payment": "Cash", "amount": 12.0}
            ],
            "return_records": [
                {
                    "name": "RET-001",
                    "return_id": "refund-001",
                    "fb_order": "ORDER-001",
                    "original_sales_invoice": "SI-001",
                    "return_sales_invoice": "SI-RET-001",
                    "refund_method": "cash",
                    "settlement_doctype": "Journal Entry",
                    "settlement_document": "JV-REFUND-001",
                    "settlement_status": "Posted",
                    "settlement_docstatus": 1,
                    "settlement_amount": 12.0,
                    "return_outstanding_amount": 0.0,
                    "settlement_gl_entries": [
                        {
                            "account": "Cash - CO",
                            "account_type": "Cash",
                            "party_type": None,
                            "debit": 0.0,
                            "credit": 12.0,
                        },
                        {
                            "account": "Debtors - CO",
                            "account_type": "Receivable",
                            "party_type": "Customer",
                            "party": "Walk-in Customer",
                            "debit": 12.0,
                            "credit": 0.0,
                            "against_voucher_type": "Sales Invoice",
                            "against_voucher": "SI-RET-001",
                        },
                    ],
                    "status": "Submitted",
                    "docstatus": 1,
                }
            ],
            "void_records": [{"sales_invoice": "SI-VOID-001", "fb_order": "ORDER-001"}],
            "projection_statuses": {
                "rows": [
                    {
                        "name": "PROJ-001",
                        "source_doctype": "FB Order",
                        "source_name": "ORDER-001",
                        "projection_type": "Sales Invoice",
                        "idempotency_key": "idem-001:Sales Invoice",
                        "target_doctype": "Sales Invoice",
                        "target_name": "SI-001",
                        "state": "Succeeded",
                        "retry_count": 0,
                    }
                ],
                "counts_by_state": {"Succeeded": 1},
                "failed": [],
            },
            "expected_cash_variance": [
                {
                    "fb_shift": "SHIFT-001",
                    "shift_code": "smoke-shift-001",
                    "status": "Closed",
                    "opening_float": 0.0,
                    "expected_cash": 0.0,
                    "counted_cash": 0.0,
                    "cash_variance": 0.0,
                }
            ],
            "idempotency": {
                "sales_invoice_counts_by_idempotency_key": {"idem-001": 1},
                "duplicate_sales_invoice_keys": [],
            },
            "legacy_active_paths": {
                "pos_invoice": {"count": 0, "records": []},
                "pos_opening_entry": {"count": 0, "records": []},
                "pos_closing_entry": {"count": 0, "records": []},
            },
            "order_history": {"invoice_count": 1},
        },
    }
