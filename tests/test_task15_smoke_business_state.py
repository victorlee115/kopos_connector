from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

smoke = importlib.import_module("kopos_connector.smoke")


def _passing_state() -> dict[str, Any]:
    return {
        "data": {
            "fb_shifts": [
                {
                    "name": "SHIFT-1",
                    "shift_code": "shift-1",
                    "status": "Closed",
                    "opening_float": 300.0,
                    "expected_cash": 300.0,
                    "counted_cash": 300.0,
                    "cash_variance": 0.0,
                }
            ],
            "fb_orders": [
                {
                    "name": "FB-ORDER-1",
                    "status": "Submitted",
                    "invoice_status": "Posted",
                    "stock_status": "Posted",
                    "sales_invoice": "SINV-1",
                    "external_idempotency_key": "idem-1",
                },
                {
                    "name": "FB-ORDER-VOID",
                    "status": "Cancelled",
                    "invoice_status": "Reversed",
                    "stock_status": "Reversed",
                    "sales_invoice": "SINV-VOID",
                    "external_idempotency_key": "void-idem-1",
                },
            ],
            "sales_invoices": [
                {
                    "name": "SINV-1",
                    "docstatus": 1,
                    "is_return": False,
                    "custom_fb_idempotency_key": "idem-1",
                    "custom_fb_shift": "SHIFT-1",
                    "items": [{"item_code": "ITEM-1"}],
                    "payments": [{"mode_of_payment": "Cash", "amount": 12.0}],
                },
                {
                    "name": "SINV-RETURN-1",
                    "docstatus": 1,
                    "is_return": True,
                    "return_against": "SINV-1",
                    "outstanding_amount": 0.0,
                    "custom_fb_shift": "SHIFT-1",
                    "payments": [],
                },
                {
                    "name": "SINV-VOID",
                    "docstatus": 2,
                    "is_return": False,
                    "custom_fb_idempotency_key": "void-idem-1",
                    "items": [{"item_code": "ITEM-1"}],
                    "payments": [{"mode_of_payment": "Cash", "amount": 12.0}],
                },
            ],
            "return_records": [
                {
                    "name": "FB-RETURN-1",
                    "return_id": "refund-1",
                    "original_sales_invoice": "SINV-1",
                    "return_sales_invoice": "SINV-RETURN-1",
                    "refund_method": "cash",
                    "settlement_doctype": "Journal Entry",
                    "settlement_document": "JV-REFUND-1",
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
                            "against_voucher": "SINV-RETURN-1",
                        },
                    ],
                    "status": "Submitted",
                    "docstatus": 1,
                }
            ],
            "void_records": [
                {
                    "sales_invoice": "SINV-VOID",
                    "fb_order": "FB-ORDER-VOID",
                    "invoice_status": "Reversed",
                    "stock_status": "Reversed",
                }
            ],
            "projection_statuses": {
                "rows": [
                    {
                        "name": "LOG-1",
                        "projection_type": "Sales Invoice",
                        "state": "Succeeded",
                    }
                ],
                "failed": [],
            },
            "legacy_active_paths": {
                "pos_invoice": {"count": 0, "records": []},
                "pos_opening_entry": {"count": 0, "records": []},
                "pos_closing_entry": {"count": 0, "records": []},
            },
            "idempotency": {
                "sales_invoice_counts_by_idempotency_key": {
                    "idem-1": 1,
                    "void-idem-1": 1,
                },
                "duplicate_sales_invoice_keys": [],
            },
            "order_history": {"source": "Sales Invoice", "invoice_count": 3},
        }
    }


def test_smoke_business_assertions_pass_for_complete_business_state() -> None:
    result = smoke.build_smoke_business_assertions(
        _passing_state(), expected_idempotency_keys=["idem-1", "void-idem-1"]
    )

    assert result["pass"] is True
    assert result["failures"] == []


def test_smoke_business_assertions_fail_on_failed_projection() -> None:
    state = _passing_state()
    failed_row = {
        "name": "LOG-FAILED",
        "projection_type": "Sales Invoice",
        "state": "Failed",
        "last_error": "forced failure",
    }
    state["data"]["projection_statuses"]["rows"].append(failed_row)
    state["data"]["projection_statuses"]["failed"].append(failed_row)

    result = smoke.build_smoke_business_assertions(
        state, expected_idempotency_keys=["idem-1"]
    )

    assert result["pass"] is False
    assert any(
        failure["assertion"] == "no_failed_projections"
        for failure in result["failures"]
    )


def test_smoke_business_assertions_fail_on_duplicate_sales_invoice_key() -> None:
    state = _passing_state()
    state["data"]["idempotency"]["sales_invoice_counts_by_idempotency_key"]["idem-1"] = 2
    state["data"]["idempotency"]["duplicate_sales_invoice_keys"] = ["idem-1"]

    result = smoke.build_smoke_business_assertions(
        state, expected_idempotency_keys=["idem-1"]
    )

    assert result["pass"] is False
    assert any(
        failure["assertion"] == "no_duplicate_sales_invoice_idempotency_keys"
        for failure in result["failures"]
    )
    assert any(
        failure["assertion"] == "exactly_one_sales_invoice_for_idem-1"
        for failure in result["failures"]
    )


def test_smoke_business_assertions_fail_on_active_legacy_path() -> None:
    state = _passing_state()
    state["data"]["legacy_active_paths"]["pos_invoice"] = {
        "count": 1,
        "records": [{"name": "POS-INV-1"}],
    }

    result = smoke.build_smoke_business_assertions(state)

    assert result["pass"] is False
    assert any(
        failure["assertion"] == "no_active_legacy_pos_paths"
        for failure in result["failures"]
    )


def test_smoke_business_assertions_fail_without_posted_refund_settlement() -> None:
    state = _passing_state()
    return_row = state["data"]["return_records"][0]
    return_row["settlement_status"] = "Pending"
    return_row["settlement_docstatus"] = 0
    return_row["return_outstanding_amount"] = -12.0
    return_row["settlement_gl_entries"] = []

    result = smoke.build_smoke_business_assertions(state)

    assert result["pass"] is False
    failed = {failure["assertion"] for failure in result["failures"]}
    assert "refund_settlement_document_posted" in failed
    assert "refund_credit_note_outstanding_zero" in failed
    assert "refund_customer_and_tender_gl_settled" in failed


def test_closed_shift_rejection_absence_passes_when_no_docs_exist(monkeypatch) -> None:
    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    result = smoke.assert_closed_shift_rejection_absence_json("closed-shift-reject-1")

    assert result["pass"] is True
    assert result["failures"] == []
    assert result["summary"] == {
        "idempotency_key": "closed-shift-reject-1",
        "fb_orders": 0,
        "sales_invoices": 0,
    }


def test_closed_shift_rejection_absence_fails_when_docs_exist(monkeypatch) -> None:
    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        if doctype == "FB Order":
            return [{"name": "FB-ORDER-BAD", "external_idempotency_key": "closed-shift-reject-1"}]
        if doctype == "Sales Invoice":
            return [{"name": "SI-BAD", "custom_fb_idempotency_key": "closed-shift-reject-1"}]
        return []

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    result = smoke.assert_closed_shift_rejection_absence_json("closed-shift-reject-1")

    assert result["pass"] is False
    assert {
        failure["assertion"] for failure in result["failures"]
    } == {
        "no_fb_order_created_for_closed_shift_rejection",
        "no_sales_invoice_created_for_closed_shift_rejection",
    }
