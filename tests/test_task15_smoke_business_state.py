from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
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
                    "opened_at": "2026-03-13 08:00:00",
                    "closed_at": "2026-03-13 18:00:00",
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
                    "ingredient_stock_entry": "STE-1",
                    "sale_datetime": "2026-03-13 10:00:00",
                    "external_idempotency_key": "idem-1",
                },
                {
                    "name": "FB-ORDER-VOID",
                    "status": "Cancelled",
                    "invoice_status": "Reversed",
                    "stock_status": "Reversed",
                    "sales_invoice": "SINV-VOID",
                    "ingredient_stock_entry": "STE-VOID",
                    "sale_datetime": "2026-03-13 11:00:00",
                    "external_idempotency_key": "void-idem-1",
                },
            ],
            "sales_invoices": [
                {
                    "name": "SINV-1",
                    "docstatus": 1,
                    "is_return": False,
                    "custom_fb_idempotency_key": "idem-1",
                    "posting_date": "2026-03-13",
                    "posting_time": "10:00:00",
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
                    "posting_date": "2026-03-13",
                    "posting_time": "11:00:00",
                    "items": [{"item_code": "ITEM-1"}],
                    "payments": [{"mode_of_payment": "Cash", "amount": 12.0}],
                },
            ],
            "ingredient_stock_entries": [
                {
                    "name": "STE-1",
                    "posting_date": "2026-03-13",
                    "posting_time": "10:00:00",
                    "custom_fb_order": "FB-ORDER-1",
                },
                {
                    "name": "STE-VOID",
                    "posting_date": "2026-03-13",
                    "posting_time": "11:00:00",
                    "custom_fb_order": "FB-ORDER-VOID",
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


def test_void_dump_preserves_sale_and_device_void_authorization_evidence() -> None:
    records = smoke._collect_void_records(
        [
            {
                "name": "SINV-VOID",
                "docstatus": 2,
                "is_return": False,
                "custom_fb_idempotency_key": "tablet-sale-1",
                "custom_fb_void_idempotency_key": "tablet-sale-1:void",
                "custom_fb_void_request_fingerprint": "a" * 64,
                "custom_fb_void_manager": "manager@example.com",
                "custom_fb_void_approval_token_id": "approval-token-1",
                "custom_fb_order": "FB-ORDER-VOID",
            }
        ],
        [
            {
                "name": "FB-ORDER-VOID",
                "sales_invoice": "SINV-VOID",
                "status": "Cancelled",
                "invoice_status": "Reversed",
                "stock_status": "Reversed",
            }
        ],
    )

    assert records == [
        {
            "sales_invoice": "SINV-VOID",
            "sale_idempotency_key": "tablet-sale-1",
            "idempotency_key": "tablet-sale-1",
            "void_idempotency_key": "tablet-sale-1:void",
            "void_request_fingerprint": "a" * 64,
            "void_manager": "manager@example.com",
            "void_approval_token_id": "approval-token-1",
            "invoice_docstatus": 2,
            "fb_order": "FB-ORDER-VOID",
            "fb_order_status": "Cancelled",
            "invoice_status": "Reversed",
            "stock_status": "Reversed",
        }
    ]


def test_sales_invoice_dump_requests_return_and_void_provenance_fields(
    monkeypatch: Any,
) -> None:
    captured_fields: list[str] = []

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        del filters, order_by
        assert doctype == "Sales Invoice"
        captured_fields.extend(fields or [])
        return []

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    assert smoke._collect_sales_invoices("DEVICE-1") == []
    assert {
        "return_against",
        "custom_fb_idempotency_key",
        "custom_fb_void_idempotency_key",
        "custom_fb_void_request_fingerprint",
        "custom_fb_void_manager",
        "custom_fb_void_approval_token_id",
    }.issubset(captured_fields)


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


def test_invoice_gl_evidence_is_exact_and_scoped_to_sales_invoice(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        captured.update(
            {
                "doctype": doctype,
                "filters": filters,
                "fields": fields,
                "order_by": order_by,
            }
        )
        return [
            {
                "name": "GL-1",
                "posting_date": "2026-03-13",
                "account": "Cash - JC",
                "account_currency": "MYR",
                "debit": "12.95",
                "credit": "0.00",
                "debit_in_account_currency": "12.95",
                "credit_in_account_currency": "0.00",
                "party_type": None,
                "party": None,
                "against": "Sales - JC",
                "voucher_type": "Sales Invoice",
                "voucher_no": "SINV-1",
                "against_voucher_type": None,
                "against_voucher": None,
                "cost_center": "Main - JC",
                "project": None,
                "remarks": "FB Order: FB-ORDER-1",
                "is_cancelled": 0,
            }
        ]

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    result = smoke._collect_invoice_gl_entries("SINV-1")

    assert captured["doctype"] == "GL Entry"
    assert captured["filters"] == {
        "voucher_type": "Sales Invoice",
        "voucher_no": "SINV-1",
    }
    assert "debit_in_account_currency" in captured["fields"]
    assert "against_voucher" in captured["fields"]
    assert result == [
        {
            "name": "GL-1",
            "posting_date": "2026-03-13",
            "account": "Cash - JC",
            "account_currency": "MYR",
            "debit": "12.95",
            "credit": "0.00",
            "debit_in_account_currency": "12.95",
            "credit_in_account_currency": "0.00",
            "party_type": None,
            "party": None,
            "against": "Sales - JC",
            "voucher_type": "Sales Invoice",
            "voucher_no": "SINV-1",
            "against_voucher_type": None,
            "against_voucher": None,
            "cost_center": "Main - JC",
            "project": None,
            "remarks": "FB Order: FB-ORDER-1",
            "is_cancelled": False,
        }
    ]


def test_manual_qr_evidence_exposes_tablet_payment_id(monkeypatch) -> None:
    def fake_get_rows(
        doctype: str,
        *,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
    ) -> list[dict[str, Any]]:
        del filters, fields, order_by
        if doctype == "Manual QR Reconciliation":
            return [
                {
                    "name": "MQR-1",
                    "fb_order_payment": "FB-PAYMENT-CHILD-1",
                    "evidence_json": '{"evidence_kind":"receipt_photo"}',
                }
            ]
        if doctype == "FB Order Payment":
            return [
                {
                    "name": "FB-PAYMENT-CHILD-1",
                    "parent": "FB-ORDER-1",
                    "source_payment_id": "tablet-payment-001",
                    "amount": "12.95",
                }
            ]
        raise AssertionError(f"unexpected doctype {doctype}")

    monkeypatch.setattr(smoke, "_get_rows", fake_get_rows)

    rows = smoke._collect_manual_qr_reconciliations("DEVICE-1")

    assert rows[0]["payment"]["name"] == "FB-PAYMENT-CHILD-1"
    assert rows[0]["payment"]["source_payment_id"] == "tablet-payment-001"
    assert rows[0]["payment"]["payment_id"] == "tablet-payment-001"
    assert rows[0]["payment"]["amount"] == "12.95"


def test_device_and_invoice_item_dump_include_accounting_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        smoke.frappe,
        "get_cached_doc",
        lambda doctype, name: SimpleNamespace(
            doctype=doctype,
            name=name,
            company="JiJi Cafe",
            customer="Walk-in Customer",
            warehouse="Main - JC",
            currency="MYR",
        ),
    )

    profile = smoke._collect_device_profile_evidence(
        SimpleNamespace(pos_profile="Counter 1")
    )
    items = smoke._collect_invoice_items(
        SimpleNamespace(
            items=[
                SimpleNamespace(
                    item_code="LATTE",
                    qty=1,
                    rate="12.00",
                    amount="12.00",
                    net_rate="12.00",
                    net_amount="12.00",
                    warehouse="Main - JC",
                    income_account="Sales - JC",
                    cost_center="Main - JC",
                    project="EVENT-1",
                    custom_fb_order_line_ref="LINE-1",
                    custom_fb_resolved_sale="RESOLVED-1",
                    custom_kopos_modifier_total="0.00",
                    custom_kopos_has_modifiers=0,
                    custom_kopos_modifiers=None,
                )
            ]
        )
    )

    assert profile == {
        "pos_profile_resolved": True,
        "pos_profile_company": "JiJi Cafe",
        "pos_profile_customer": "Walk-in Customer",
        "pos_profile_warehouse": "Main - JC",
        "pos_profile_currency": "MYR",
    }
    assert items[0]["income_account"] == "Sales - JC"
    assert items[0]["cost_center"] == "Main - JC"
    assert items[0]["project"] == "EVENT-1"
