from __future__ import annotations

import importlib
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .fake_frappe import install_fake_frappe_modules


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

install_fake_frappe_modules()

smoke = importlib.import_module("kopos_connector.smoke")


def test_maybank_smoke_policy_treats_paid_at_as_observation_time() -> None:
    policy = smoke._maybank_qr_policy()
    contract = smoke._maybank_qr_contract()

    assert policy["provider_paid_is_authoritative"] is True
    assert policy["paid_at_semantics"] == "first_provider_paid_observation_at_erp"
    assert policy["expiry_grace_is_acceptance_gate"] is False
    assert contract == {
        "version": "provider_paid_observation_v1",
        "provider_paid_status_code": 1,
        "provider_paid_is_authoritative": True,
        "provider_paid_at_available": False,
        "provider_paid_at_source": "not_supplied_by_provider_status_contract",
        "provider_status_observed_at_field": "paid_at",
        "qr_expiry_is_not_settlement_rejection": True,
    }


def test_maybank_dump_proves_late_authenticated_paid_consumption(
    monkeypatch,
) -> None:
    transaction = {
        "name": "MBQR-LATE-1",
        "transaction_refno": "PROVIDER-LATE-1",
        "status": "paid",
        "maybank_status": 1,
        "sale_amount": "12.50",
        "sale_amount_sen": 1250,
        "fb_order": "FB-ORDER-LATE-1",
        "sales_invoice": "SINV-LATE-1",
        "outlet_id": "OUTLET-1",
        "device_id": "SMOKE-TAB-A001",
        "provider": "maybank_qr",
        "currency": "MYR",
        "idempotency_key": "late-idempotency-1",
        "request_fingerprint": "f" * 64,
        "consumption_key": "FB-ORDER-LATE-1",
        "invoice_consumption_key": "SINV-LATE-1",
        "paid_at": datetime(2026, 7, 16, 20, 0, 0),
        "consumed_at": datetime(2026, 7, 16, 20, 0, 1),
        "expires_at": datetime(2026, 7, 16, 12, 0, 0),
        "last_polled_at": datetime(2026, 7, 16, 20, 0, 0),
    }
    payment = {
        "name": "PAY-LATE-1",
        "parent": "FB-ORDER-LATE-1",
        "source_payment_id": "mobile-payment-late-1",
        "amount": "12.50",
        "external_transaction_id": "PROVIDER-LATE-1",
        "maybank_qr_transaction": "MBQR-LATE-1",
    }

    def get_rows(doctype, **_kwargs):
        if doctype == "Maybank QR Transaction":
            return [dict(transaction)]
        if doctype == "FB Order Payment":
            return [dict(payment)]
        raise AssertionError(f"unexpected smoke query: {doctype}")

    monkeypatch.setattr(smoke, "_get_rows", get_rows)

    rows = smoke._collect_maybank_qr_transactions("SMOKE-TAB-A001")

    assert len(rows) == 1
    row = rows[0]
    assert row["provider_paid_at"] is None
    assert row["provider_status_observed_at"] == transaction["paid_at"]
    assert row["paid_observed_seconds_after_expiry"] == 8 * 60 * 60
    assert row["provider_paid_authoritative"] is True
    assert row["provider_identity_evidence_complete"] is True
    assert row["one_time_consumption_evidence_complete"] is True
    assert row["late_authenticated_paid_accepted"] is True


def test_delayed_maybank_smoke_requires_explicit_mock_developer_context(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        smoke.frappe,
        "conf",
        SimpleNamespace(allow_maybank_mock=0, developer_mode=1),
        raising=False,
    )

    try:
        smoke._require_maybank_mock_smoke_context()
    except smoke.frappe.ValidationError as error:
        assert "allow_maybank_mock=1" in str(error)
    else:
        raise AssertionError("smoke context must fail closed without explicit opt-in")

    monkeypatch.setattr(
        smoke.frappe,
        "conf",
        SimpleNamespace(allow_maybank_mock=1, developer_mode=1),
        raising=False,
    )
    smoke._require_maybank_mock_smoke_context()


def test_delayed_maybank_smoke_refuses_non_smoke_outlet(monkeypatch) -> None:
    writes: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        smoke.frappe.db,
        "get_single_value",
        lambda *_args: "LIVE-OUTLET-DO-NOT-TOUCH",
    )
    monkeypatch.setattr(
        smoke.frappe.db,
        "set_single_value",
        lambda *args: writes.append(args),
        raising=False,
    )

    try:
        smoke._ensure_smoke_maybank_outlet()
    except smoke.frappe.ValidationError as error:
        assert "refuses to overwrite" in str(error)
    else:
        raise AssertionError("smoke helper must refuse a non-smoke outlet")

    assert writes == []


def test_smoke_rejects_unresolved_or_unproved_duplicate_qr_liability() -> None:
    unresolved_state = _passing_state()
    unresolved_state["data"]["maybank_qr_transactions"] = [
        {
            "name": "MBQR-DUPLICATE-1",
            "duplicate_payment_status": "refund_required",
            "duplicate_accounting_integrity_proven": True,
        }
    ]

    unresolved = smoke.build_smoke_business_assertions(unresolved_state)

    assert (
        unresolved["assertions"]["duplicate_qr_accounting_integrity_proven"]
        is True
    )
    assert (
        unresolved["assertions"][
            "no_unresolved_duplicate_qr_customer_liabilities"
        ]
        is False
    )

    unproved_state = _passing_state()
    unproved_state["data"]["maybank_qr_transactions"] = [
        {
            "name": "MBQR-DUPLICATE-1",
            "duplicate_payment_status": "refunded",
            "duplicate_accounting_integrity_proven": False,
        }
    ]

    unproved = smoke.build_smoke_business_assertions(unproved_state)

    assert (
        unproved["assertions"]["duplicate_qr_accounting_integrity_proven"]
        is False
    )
    assert (
        unproved["assertions"][
            "no_unresolved_duplicate_qr_customer_liabilities"
        ]
        is True
    )


def test_smoke_duplicate_qr_integrity_requires_live_exact_journals_and_file() -> None:
    row: dict[str, Any] = {
        "name": "MBQR-DUPLICATE-1",
        "device_id": "SMOKE-TAB-A001",
        "transaction_refno": "MBB-DUPLICATE-1",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "PAY-1",
        "winning_sales_invoice": "SINV-1",
        "winning_company": "KoPOS Malaysia Sdn Bhd",
        "winning_sale": {
            "name": "FB-ORDER-1",
            "docstatus": 1,
            "sales_invoice": "SINV-1",
            "external_idempotency_key": "SALE-IDEMPOTENCY-1",
            "device_id": "SMOKE-TAB-A001",
            "status": "Submitted",
            "invoice_status": "Posted",
            "company": "KoPOS Malaysia Sdn Bhd",
            "currency": "MYR",
            "automatic_qr_state": "finalized",
            "automatic_qr_payment": "PAY-1",
            "automatic_qr_winner_channel": "maybank_qr",
            "automatic_qr_static_reconciliation": None,
        },
        "winning_invoice": {
            "name": "SINV-1",
            "docstatus": 1,
            "is_return": 0,
            "custom_fb_order": "FB-ORDER-1",
            "custom_fb_idempotency_key": "SALE-IDEMPOTENCY-1",
            "custom_fb_device_id": "SMOKE-TAB-A001",
            "custom_fb_void_idempotency_key": None,
            "custom_fb_void_request_fingerprint": None,
            "custom_fb_void_manager": None,
            "custom_fb_void_approval_token_id": None,
            "company": "KoPOS Malaysia Sdn Bhd",
            "currency": "MYR",
            "void_approval": None,
        },
        "duplicate_payment_status": "refunded",
        "duplicate_winning_channel": "maybank_qr",
        "duplicate_winning_transaction": "MBQR-WINNER-1",
        "duplicate_winning_static_reconciliation": None,
        "duplicate_accounting_key": "LIABILITY-KEY-1",
        "duplicate_liability_journal_entry": "JV-liability_recognition",
        "duplicate_refund_key": "REFUND-KEY-1",
        "duplicate_refund_journal_entry": "JV-refund",
        "duplicate_clearing_account": "Maybank Clearing - KMY",
        "duplicate_liability_account": "Customer Refund Liability - KMY",
        "duplicate_refund_reference": "MBB-REFUND-1",
        "duplicate_refund_evidence_reference": "MAYBANK-CASE-1",
        "duplicate_refund_evidence_file": "FILE-1",
        "duplicate_refund_evidence_sha256": "a" * 64,
        "duplicate_refund_amount_sen": 1250,
        "duplicate_refund_currency": "MYR",
        "duplicate_refund_date": "2026-03-13",
        "duplicate_refund_note": (
            "Provider portal confirms the exact duplicate payment refund."
        ),
        "duplicate_refunded_by": "manager@example.test",
        "duplicate_refunded_at": "2026-03-13 12:00:00",
        "sale_amount_sen": 1250,
        "currency": "MYR",
        "paid_at": "2026-03-12 23:59:00",
        "duplicate_refund_evidence_file_proof": {
            "name": "FILE-1",
            "is_private": True,
            "attached_to_doctype": "Maybank QR Transaction",
            "attached_to_name": "MBQR-DUPLICATE-1",
            "declared_size": 8,
            "observed_size": 8,
            "observed_sha256": "a" * 64,
        },
    }

    def journal(
        *,
        key: str,
        stage: str,
        posting_date: str,
        evidence_reference: str,
        evidence_file: str | None,
        evidence_sha256: str | None,
        debit_account: str,
        credit_account: str,
    ) -> dict[str, Any]:
        return {
            "name": f"JV-{stage}",
            "docstatus": 1,
            "posting_date": posting_date,
            "company": row["winning_company"],
            "custom_kopos_qr_duplicate_key": key,
            "custom_kopos_qr_duplicate_stage": stage,
            "custom_kopos_qr_provider_transaction": row["name"],
            "custom_kopos_qr_winning_transaction": row[
                "duplicate_winning_transaction"
            ],
            "custom_kopos_qr_winning_channel": row[
                "duplicate_winning_channel"
            ],
            "custom_kopos_qr_winning_static_reconciliation": row[
                "duplicate_winning_static_reconciliation"
            ],
            "custom_kopos_qr_provider_evidence_reference": evidence_reference,
            "custom_kopos_qr_provider_evidence_file": evidence_file,
            "custom_kopos_qr_provider_evidence_sha256": evidence_sha256,
            "custom_kopos_qr_source_doctype": "Maybank QR Transaction",
            "custom_kopos_qr_source_name": row["name"],
            "custom_kopos_qr_fb_order": row["fb_order"],
            "custom_kopos_qr_sales_invoice": row["winning_sales_invoice"],
            "custom_kopos_qr_amount_sen": row["sale_amount_sen"],
            "custom_kopos_qr_currency": row["currency"],
            "gl_entries": [
                {"account": debit_account, "debit": "12.50", "credit": "0"},
                {"account": credit_account, "debit": "0", "credit": "12.50"},
            ],
        }

    row["duplicate_liability_journal"] = journal(
        key="LIABILITY-KEY-1",
        stage="liability_recognition",
        posting_date="2026-03-12",
        evidence_reference="MBB-DUPLICATE-1",
        evidence_file=None,
        evidence_sha256=None,
        debit_account="Maybank Clearing - KMY",
        credit_account="Customer Refund Liability - KMY",
    )
    row["duplicate_refund_journal"] = journal(
        key="REFUND-KEY-1",
        stage="refund",
        posting_date="2026-03-13",
        evidence_reference="MAYBANK-CASE-1",
        evidence_file="FILE-1",
        evidence_sha256="a" * 64,
        debit_account="Customer Refund Liability - KMY",
        credit_account="Maybank Clearing - KMY",
    )

    assert smoke._duplicate_qr_accounting_integrity_proven(row) is True

    row["duplicate_refund_journal"]["docstatus"] = 2
    assert smoke._duplicate_qr_accounting_integrity_proven(row) is False

    row["duplicate_refund_journal"]["docstatus"] = 1
    row["winning_invoice"]["docstatus"] = 2
    row["winning_sale"]["status"] = "Cancelled"
    row["winning_sale"]["invoice_status"] = "Reversed"
    assert smoke._duplicate_qr_accounting_integrity_proven(row) is False

    row["winning_invoice"].update(
        {
            "custom_fb_void_idempotency_key": "VOID-IDEMPOTENCY-1",
            "custom_fb_void_request_fingerprint": "d" * 64,
            "custom_fb_void_manager": "manager@example.test",
            "custom_fb_void_approval_token_id": "APPROVAL-TOKEN-1",
            "void_approval": {
                "token_id": "APPROVAL-TOKEN-1",
                "status": "consumed",
                "manager_id": "manager@example.test",
                "action": "void_order",
                "resource_id": "SINV-1",
                "context_hash": "c" * 64,
                "consumed_idempotency_key": "VOID-IDEMPOTENCY-1",
            },
        }
    )
    assert smoke._duplicate_qr_accounting_integrity_proven(row) is True


def test_smoke_duplicate_qr_integrity_proves_exact_static_winner() -> None:
    row: dict[str, Any] = {
        "name": "MBQR-DUPLICATE-STATIC-1",
        "device_id": "SMOKE-TAB-A001",
        "transaction_refno": "MBB-DUPLICATE-STATIC-1",
        "fb_order": "FB-ORDER-STATIC-1",
        "fb_order_payment": "PAY-STATIC-1",
        "winning_sales_invoice": "SINV-STATIC-1",
        "winning_company": "KoPOS Malaysia Sdn Bhd",
        "winning_sale": {
            "name": "FB-ORDER-STATIC-1",
            "docstatus": 1,
            "sales_invoice": "SINV-STATIC-1",
            "external_idempotency_key": "SALE-STATIC-1",
            "device_id": "SMOKE-TAB-A001",
            "status": "Submitted",
            "invoice_status": "Posted",
            "company": "KoPOS Malaysia Sdn Bhd",
            "currency": "MYR",
            "automatic_qr_state": "finalized",
            "automatic_qr_payment": "PAY-STATIC-1",
            "automatic_qr_winner_channel": "static_qr",
            "automatic_qr_static_reconciliation": "MQR-STATIC-1",
        },
        "winning_invoice": {
            "name": "SINV-STATIC-1",
            "docstatus": 1,
            "is_return": 0,
            "custom_fb_order": "FB-ORDER-STATIC-1",
            "custom_fb_idempotency_key": "SALE-STATIC-1",
            "custom_fb_device_id": "SMOKE-TAB-A001",
            "custom_fb_void_idempotency_key": None,
            "custom_fb_void_request_fingerprint": None,
            "custom_fb_void_manager": None,
            "custom_fb_void_approval_token_id": None,
            "company": "KoPOS Malaysia Sdn Bhd",
            "currency": "MYR",
        },
        "winning_static_reconciliation": {
            "name": "MQR-STATIC-1",
            "status": "pending_reconciliation",
            "fb_order": "FB-ORDER-STATIC-1",
            "fb_order_payment": "PAY-STATIC-1",
            "sales_invoice": "SINV-STATIC-1",
            "device_id": "SMOKE-TAB-A001",
            "company": "KoPOS Malaysia Sdn Bhd",
            "currency": "MYR",
            "amount_sen": 1250,
            "provider_session_id": "static-session-1",
            "reconciliation_idempotency_key": "static-recon-1",
        },
        "duplicate_payment_status": "refund_required",
        "duplicate_winning_channel": "static_qr",
        "duplicate_winning_transaction": None,
        "duplicate_winning_static_reconciliation": "MQR-STATIC-1",
        "duplicate_accounting_key": "LIABILITY-STATIC-1",
        "duplicate_liability_journal_entry": "JV-STATIC-1",
        "duplicate_refund_journal_entry": None,
        "duplicate_clearing_account": "Maybank Clearing - KMY",
        "duplicate_liability_account": "Customer Refund Liability - KMY",
        "sale_amount_sen": 1250,
        "currency": "MYR",
        "paid_at": "2026-03-12 23:59:00",
    }
    row["duplicate_liability_journal"] = {
        "name": "JV-STATIC-1",
        "docstatus": 1,
        "posting_date": "2026-03-12",
        "company": row["winning_company"],
        "custom_kopos_qr_duplicate_key": "LIABILITY-STATIC-1",
        "custom_kopos_qr_duplicate_stage": "liability_recognition",
        "custom_kopos_qr_provider_transaction": row["name"],
        "custom_kopos_qr_winning_transaction": None,
        "custom_kopos_qr_winning_channel": "static_qr",
        "custom_kopos_qr_winning_static_reconciliation": "MQR-STATIC-1",
        "custom_kopos_qr_provider_evidence_reference": row[
            "transaction_refno"
        ],
        "custom_kopos_qr_provider_evidence_file": None,
        "custom_kopos_qr_provider_evidence_sha256": None,
        "custom_kopos_qr_source_doctype": "Maybank QR Transaction",
        "custom_kopos_qr_source_name": row["name"],
        "custom_kopos_qr_fb_order": row["fb_order"],
        "custom_kopos_qr_sales_invoice": row["winning_sales_invoice"],
        "custom_kopos_qr_amount_sen": row["sale_amount_sen"],
        "custom_kopos_qr_currency": row["currency"],
        "gl_entries": [
            {
                "account": "Maybank Clearing - KMY",
                "debit": "12.50",
                "credit": "0",
            },
            {
                "account": "Customer Refund Liability - KMY",
                "debit": "0",
                "credit": "12.50",
            },
        ],
    }

    assert smoke._duplicate_qr_accounting_integrity_proven(row) is True

    row["winning_static_reconciliation"]["amount_sen"] = 1249
    assert smoke._duplicate_qr_accounting_integrity_proven(row) is False


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
        failure["assertion"] == "no_failed_commercial_projections"
        for failure in result["failures"]
    )


def test_smoke_business_assertions_do_not_treat_inventory_as_release_scope() -> None:
    state = _passing_state()
    failed_row = {
        "name": "LOG-STOCK-FAILED",
        "projection_type": "Stock Issue",
        "state": "Failed",
        "last_error": "inventory subsystem unavailable",
    }
    state["data"]["projection_statuses"]["rows"].append(failed_row)
    state["data"]["projection_statuses"]["failed"].append(failed_row)

    result = smoke.build_smoke_business_assertions(
        state, expected_idempotency_keys=["idem-1"]
    )

    assert result["pass"] is True
    assert result["summary"]["inventory_acceptance"] is False
    assert result["assertions"]["no_failed_commercial_projections"] is True


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
    profile_values = {
        "company": "JiJi Cafe",
        "customer": "Walk-in Customer",
        "warehouse": "Main - JC",
        "currency": "MYR",
        "custom_kopos_enable_sst": 0,
        "custom_kopos_sst_rate": 8,
    }
    profile = SimpleNamespace(
        doctype="POS Profile",
        name="Counter 1",
        **profile_values,
        as_dict=lambda: dict(profile_values),
    )
    monkeypatch.setattr(smoke.frappe, "get_cached_doc", lambda *_args: profile)
    monkeypatch.setattr(smoke.frappe, "get_doc", lambda *_args: profile)

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
        "pos_profile_sst_enabled": False,
        "pos_profile_sst_rate_percent": 8.0,
        "device_config_tax_rate": 0.0,
    }
    assert items[0]["income_account"] == "Sales - JC"
    assert items[0]["cost_center"] == "Main - JC"
    assert items[0]["project"] == "EVENT-1"
