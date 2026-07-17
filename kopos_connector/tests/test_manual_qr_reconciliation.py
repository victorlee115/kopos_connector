# pyright: reportMissingImports=false

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from kopos_connector.tests.fake_frappe import install_fake_frappe_modules


@dataclass
class ReconciliationEnv:
    transactions: dict[str, SimpleNamespace] = field(default_factory=dict)
    manual_reconciliations: dict[str, SimpleNamespace] = field(default_factory=dict)
    comments: list[SimpleNamespace] = field(default_factory=list)
    updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    payment_updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    accounting_posts: list[str] = field(default_factory=list)
    failure_accounting_posts: list[tuple[str, str]] = field(default_factory=list)
    failure_accounting_assertions: list[tuple[str, str]] = field(default_factory=list)


@pytest.fixture
def reconciliation_module(monkeypatch):
    install_fake_frappe_modules()

    import frappe
    import kopos_connector.api.manual_qr_receipt as manual_qr_receipt

    env = ReconciliationEnv(
        transactions={
            "MBQR-PENDING-1": build_transaction(
                name="MBQR-PENDING-1",
                transaction_refno="TXN-PENDING-1",
                device_id="DEVICE-A",
                amount_sen=1200,
                manual_reconciliation_status="pending_reconciliation",
                receipt_file="FILE-1",
                receipt_uploaded_at=datetime(2026, 3, 13, 18, 6, 0),
                receipt_idempotency_key="receipt-key-1",
                receipt_payment_id="PAY-1",
                receipt_order_id="ORDER-1",
                fb_order="FB-ORDER-1",
                fb_order_payment="FB-PAY-1",
                sales_invoice="SINV-1",
            ),
            "MBQR-PENDING-2": build_transaction(
                name="MBQR-PENDING-2",
                transaction_refno="TXN-PENDING-2",
                device_id="DEVICE-B",
                amount_sen=2500,
                manual_reconciliation_status="pending_reconciliation",
                receipt_file="FILE-2",
                receipt_uploaded_at=datetime(2026, 3, 13, 18, 6, 30),
                receipt_idempotency_key="receipt-key-2",
                receipt_payment_id="PAY-2",
                receipt_order_id="ORDER-2",
                fb_order="FB-ORDER-2",
                fb_order_payment="FB-PAY-2",
                sales_invoice="SINV-2",
            ),
            "MBQR-PENDING-NO-RECEIPT": build_transaction(
                name="MBQR-PENDING-NO-RECEIPT",
                transaction_refno="TXN-PENDING-NO-RECEIPT",
                device_id="DEVICE-A",
                amount_sen=1800,
                manual_reconciliation_status="pending_reconciliation",
                fb_order="FB-ORDER-3",
                fb_order_payment="FB-PAY-3",
                sales_invoice="SINV-3",
            ),
            "MBQR-LEGACY-FALSE-PENDING": build_transaction(
                name="MBQR-LEGACY-FALSE-PENDING",
                transaction_refno="TXN-LEGACY-FALSE-PENDING",
                device_id="DEVICE-A",
                amount_sen=900,
                manual_reconciliation_status="pending_reconciliation",
            ),
            "MBQR-NORMAL": build_transaction(
                name="MBQR-NORMAL",
                transaction_refno="TXN-NORMAL",
                device_id="DEVICE-A",
                amount_sen=1200,
            ),
            "MBQR-RECONCILED": build_transaction(
                name="MBQR-RECONCILED",
                transaction_refno="TXN-RECONCILED",
                device_id="DEVICE-A",
                amount_sen=1200,
                manual_reconciliation_status="reconciled",
                reconciled_by="manager-a",
            ),
            "MBQR-FAILED": build_transaction(
                name="MBQR-FAILED",
                transaction_refno="TXN-FAILED",
                device_id="DEVICE-A",
                amount_sen=1200,
                manual_reconciliation_status="reconciliation_failed",
                reconciliation_failed_reason="amount_mismatch",
            ),
        }
    )

    def fake_get_all(
        doctype: str,
        filters: dict[str, Any] | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        records = (
            env.manual_reconciliations
            if doctype == "Manual QR Reconciliation"
            else env.transactions
        )
        rows = []
        for txn in records.values():
            if filters:
                matches = True
                for key, value in filters.items():
                    actual = getattr(txn, key, None)
                    if isinstance(value, list) and len(value) == 2 and value[0] == "in":
                        matches = actual in value[1]
                    else:
                        matches = actual == value
                    if not matches:
                        break
                if not matches:
                    continue
            rows.append({field: getattr(txn, field, None) for field in fields or []})
        if order_by == "created_at asc":
            rows.sort(key=lambda row: row.get("created_at") or datetime.min)
        return rows

    def fake_get_value(
        doctype: str,
        filters: Any = None,
        fieldname: Any = None,
        **_kwargs: Any,
    ) -> Any:
        if doctype == "FB Order Payment" and isinstance(filters, str):
            payment_parents = {
                "FB-PAY-1": "FB-ORDER-1",
                "FB-PAY-2": "FB-ORDER-2",
                "FB-PAY-3": "FB-ORDER-3",
            }
            parent = payment_parents.get(filters)
            if fieldname == ["parent", "suspense_account"] and parent:
                return {
                    "parent": parent,
                    "suspense_account": "Manual QR Suspense - KPC",
                }
        records = (
            env.manual_reconciliations
            if doctype == "Manual QR Reconciliation"
            else env.transactions
        )
        if isinstance(filters, dict):
            for txn in records.values():
                if all(getattr(txn, key, None) == value for key, value in filters.items()):
                    return getattr(txn, fieldname)
        return None

    def fake_set_value(doctype: str, name: str, values: dict[str, Any], **_kwargs: Any) -> None:
        if doctype == "FB Order Payment":
            env.payment_updates.append((name, values))
            return
        records = (
            env.manual_reconciliations
            if doctype == "Manual QR Reconciliation"
            else env.transactions
        )
        txn = records[name]
        for key, value in values.items():
            setattr(txn, key, value)
        env.updates.append((name, values))

    def fake_get_doc(*args: Any, **kwargs: Any) -> Any:
        if len(args) == 1 and isinstance(args[0], dict):
            payload = args[0]
            if payload.get("doctype") == "Comment":
                return FakeCommentDoc(env, payload)
        if len(args) >= 2 and args[0] == "Maybank QR Transaction":
            return env.transactions[str(args[1])]
        if len(args) >= 2 and args[0] == "Manual QR Reconciliation":
            return env.manual_reconciliations[str(args[1])]
        if len(args) >= 2 and args[0] == "FB Order":
            return SimpleNamespace(
                doctype="FB Order",
                name=str(args[1]),
                company="KoPOS Cafe",
                currency="MYR",
                docstatus=1,
            )
        raise AssertionError(f"unexpected get_doc call: {args!r} {kwargs!r}")

    def fake_ensure_reclassification(source: Any) -> dict[str, Any]:
        assert manual_qr_receipt._record_status(source) == "pending_reconciliation"
        if not manual_qr_receipt._is_static_reconciliation(source):
            assert source.company == "KoPOS Cafe"
            assert source.currency == "MYR"
            assert source.suspense_account == "Manual QR Suspense - KPC"
        env.accounting_posts.append(source.name)
        return {"journal_entry": f"JV-QR-{source.name}"}

    def fake_ensure_failure_reclassification(
        source: Any,
        reason: str,
    ) -> dict[str, Any]:
        assert manual_qr_receipt._record_status(source) == "pending_reconciliation"
        journal_entry = f"JV-QR-FAIL-{source.name}"
        source.failure_accounting_key = f"kopos:qr-failure:v1:{source.name}"
        source.failure_variance_account = "QR Failure Variance - KPC"
        source.failure_cost_center = "Main - KPC"
        source.failure_accounting_reason = reason
        source.failure_journal_entry = journal_entry
        env.failure_accounting_posts.append((source.name, reason))
        return {"journal_entry": journal_entry, "failure_reason": reason}

    def fake_assert_failure_reclassification(
        source: Any,
        reason: str,
    ) -> dict[str, Any]:
        assert manual_qr_receipt._record_status(source) == "reconciliation_failed"
        env.failure_accounting_assertions.append((source.name, reason))
        return {
            "journal_entry": source.failure_journal_entry,
            "failure_reason": reason,
        }

    monkeypatch.setattr(frappe.db, "get_value", fake_get_value, raising=False)
    monkeypatch.setattr(frappe.db, "set_value", fake_set_value, raising=False)
    monkeypatch.setattr(frappe, "get_all", fake_get_all, raising=False)
    monkeypatch.setattr(frappe, "get_doc", fake_get_doc, raising=False)
    monkeypatch.setattr(frappe, "as_json", json.dumps, raising=False)
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="manager@example.com"), raising=False)
    monkeypatch.setattr(
        frappe,
        "get_roles",
        lambda user=None: ["System Manager"] if user == "manager@example.com" else [],
        raising=False,
    )
    monkeypatch.setattr(
        manual_qr_receipt,
        "ensure_qr_suspense_reclassification",
        fake_ensure_reclassification,
    )
    monkeypatch.setattr(
        manual_qr_receipt,
        "ensure_qr_suspense_failure_reclassification",
        fake_ensure_failure_reclassification,
    )
    monkeypatch.setattr(
        manual_qr_receipt,
        "assert_qr_suspense_failure_reclassification",
        fake_assert_failure_reclassification,
    )
    return SimpleNamespace(module=manual_qr_receipt, env=env, frappe=frappe)


def test_list_returns_only_pending_reconciliation_transactions(reconciliation_module):
    rows = reconciliation_module.module.list_pending_manual_qr_reconciliations()

    assert [row["transaction_refno"] for row in rows] == [
        "TXN-PENDING-1",
        "TXN-PENDING-2",
        "TXN-PENDING-NO-RECEIPT",
    ]
    assert rows[0]["manual_reconciliation_status"] == "pending_reconciliation"
    assert rows[0]["device_id"] == "DEVICE-A"
    assert rows[0]["sale_amount_sen"] == 1200
    assert rows[0]["business_date"] == "2026-03-13"
    assert rows[0]["provider"] == "maybank_qr"
    assert rows[0]["receipt_file"] == "FILE-1"
    assert rows[0]["receipt_uploaded_at"] == datetime(2026, 3, 13, 18, 6, 0)
    assert rows[0]["receipt_idempotency_key"] == "receipt-key-1"
    assert rows[0]["receipt_payment_id"] == "PAY-1"
    assert rows[0]["receipt_order_id"] == "ORDER-1"
    assert rows[0]["fb_order"] == "FB-ORDER-1"
    assert rows[0]["sales_invoice"] == "SINV-1"
    assert "TXN-NORMAL" not in {row["transaction_refno"] for row in rows}
    assert "TXN-LEGACY-FALSE-PENDING" not in {
        row["transaction_refno"] for row in rows
    }


def test_fetch_manual_qr_reconciliation_status_returns_requested_rows(reconciliation_module):
    result = reconciliation_module.module.fetch_manual_qr_reconciliation_status(
        payments=[
            {"payment_id": "PAY-1", "provider_session_id": "TXN-PENDING-1"},
            {"payment_id": "PAY-2", "provider_session_id": "TXN-RECONCILED"},
            {"payment_id": "PAY-3", "provider_session_id": "TXN-NORMAL"},
        ]
    )

    assert result["statuses"] == [
        {
            "payment_id": "PAY-1",
            "provider_session_id": "TXN-PENDING-1",
            "transaction_refno": "TXN-PENDING-1",
            "reconciliation_status": "pending_reconciliation",
            "reconciled_by": None,
            "reconciled_at": None,
            "reconciliation_note": None,
            "reconciliation_failed_reason": None,
        },
        {
            "payment_id": "PAY-2",
            "provider_session_id": "TXN-RECONCILED",
            "transaction_refno": "TXN-RECONCILED",
            "reconciliation_status": "reconciled",
            "reconciled_by": "manager-a",
            "reconciled_at": None,
            "reconciliation_note": None,
            "reconciliation_failed_reason": None,
        },
        {
            "payment_id": "PAY-3",
            "provider_session_id": "TXN-NORMAL",
            "transaction_refno": "TXN-NORMAL",
            "reconciliation_status": None,
            "reconciled_by": None,
            "reconciled_at": None,
            "reconciliation_note": None,
            "reconciliation_failed_reason": None,
        },
    ]


def test_static_qr_list_fetch_and_reconcile_route_to_manual_record(
    reconciliation_module,
):
    reconciliation = build_manual_reconciliation()
    reconciliation_module.env.manual_reconciliations[reconciliation.name] = reconciliation

    listed = reconciliation_module.module.list_pending_manual_qr_reconciliations()
    fetched = reconciliation_module.module.fetch_manual_qr_reconciliation_status(
        payments=[
            {"payment_id": "PAY-STATIC", "provider_session_id": "static-session-1"}
        ]
    )
    result = reconciliation_module.module.mark_manual_qr_reconciled(
        transaction_refno="static-session-1",
        amount_sen="1200",
        business_date="2026-03-13",
        device_id="DEVICE-A",
        provider="static_qr",
        manager_id="manager@example.com",
        note="Matched static QR bank settlement",
    )

    assert "static-session-1" in {row["transaction_refno"] for row in listed}
    static_row = next(
        row for row in listed if row["transaction_refno"] == "static-session-1"
    )
    assert static_row["provider"] == "static_qr"
    assert static_row["business_date"] == "2026-03-13"
    assert fetched["statuses"][0]["reconciliation_status"] == (
        "pending_reconciliation"
    )
    assert result == {
        "status": "ok",
        "manual_reconciliation_status": "reconciled",
        "reclassification_journal_entry": "JV-QR-MQR-1",
    }
    assert reconciliation.status == "reconciled"
    assert reconciliation.reclassification_journal_entry == "JV-QR-MQR-1"
    assert reconciliation_module.env.accounting_posts == ["MQR-1"]
    assert reconciliation_module.env.payment_updates == [
        ("FB-PAY-STATIC", {"settlement_status": "reconciled"})
    ]
    assert reconciliation_module.env.comments[-1].reference_doctype == (
        "Manual QR Reconciliation"
    )


def test_static_qr_failure_updates_order_payment_settlement(reconciliation_module):
    reconciliation = build_manual_reconciliation()
    reconciliation_module.env.manual_reconciliations[reconciliation.name] = reconciliation

    result = reconciliation_module.module.mark_manual_qr_reconciliation_failed(
        transaction_refno="static-session-1",
        amount_sen="1200",
        business_date="2026-03-13",
        device_id="DEVICE-A",
        provider="static_qr",
        manager_id="manager@example.com",
        reason="no_bank_transaction",
        note="No matching static QR bank settlement",
    )

    assert result == {
        "status": "ok",
        "manual_reconciliation_status": "reconciliation_failed",
        "failure_journal_entry": "JV-QR-FAIL-MQR-1",
    }
    assert reconciliation.status == "reconciliation_failed"
    assert reconciliation.failure_journal_entry == "JV-QR-FAIL-MQR-1"
    assert reconciliation_module.env.payment_updates == [
        ("FB-PAY-STATIC", {"settlement_status": "reconciliation_failed"})
    ]
    assert reconciliation_module.env.accounting_posts == []
    assert reconciliation_module.env.failure_accounting_posts == [
        ("MQR-1", "no_bank_transaction")
    ]


def test_fetch_manual_qr_reconciliation_status_accepts_transaction_refnos(reconciliation_module):
    result = reconciliation_module.module.fetch_manual_qr_reconciliation_status(
        transaction_refnos=["TXN-FAILED"]
    )

    assert result["statuses"][0]["transaction_refno"] == "TXN-FAILED"
    assert result["statuses"][0]["reconciliation_status"] == "reconciliation_failed"
    assert result["statuses"][0]["reconciliation_failed_reason"] == "amount_mismatch"


def test_fetch_manual_qr_reconciliation_status_scopes_device_user(reconciliation_module, monkeypatch):
    monkeypatch.setattr(
        reconciliation_module.frappe,
        "session",
        SimpleNamespace(user="device-a@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        reconciliation_module.frappe,
        "get_roles",
        lambda user=None: ["KoPOS Device API"] if user == "device-a@example.com" else [],
        raising=False,
    )
    monkeypatch.setattr(
        reconciliation_module.module,
        "get_authenticated_device_doc",
        lambda: SimpleNamespace(device_id="DEVICE-A"),
    )

    result = reconciliation_module.module.fetch_manual_qr_reconciliation_status(
        payments=[
            {"payment_id": "PAY-A", "provider_session_id": "TXN-RECONCILED"},
            {"payment_id": "PAY-B", "provider_session_id": "TXN-PENDING-2"},
        ]
    )

    assert result["statuses"][0]["reconciliation_status"] == "reconciled"
    assert result["statuses"][1]["transaction_refno"] == "TXN-PENDING-2"
    assert result["statuses"][1]["reconciliation_status"] is None
    assert result["statuses"][1]["reconciled_by"] is None


def test_fetch_manual_qr_reconciliation_status_rejects_non_device_user(reconciliation_module, monkeypatch):
    monkeypatch.setattr(
        reconciliation_module.frappe,
        "session",
        SimpleNamespace(user="cashier@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        reconciliation_module.frappe,
        "get_roles",
        lambda user=None: ["POS User"] if user == "cashier@example.com" else [],
        raising=False,
    )

    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.fetch_manual_qr_reconciliation_status(
            transaction_refnos=["TXN-RECONCILED"]
        )

    assert "not allowed to access KoPOS APIs" in str(excinfo.value)


def test_mark_reconciled_updates_status_and_audit(reconciliation_module):
    result = reconciliation_module.module.mark_manual_qr_reconciled(
        transaction_refno="TXN-PENDING-1",
        amount_sen="1200",
        business_date="2026-03-13",
        device_id="DEVICE-A",
        provider="maybank_qr",
        manager_id="manager@example.com",
        note="Matched Maybank settlement report",
    )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    comment = reconciliation_module.env.comments[0]
    audit = json.loads(comment.content)

    assert result == {
        "status": "ok",
        "manual_reconciliation_status": "reconciled",
        "reclassification_journal_entry": "JV-QR-MBQR-PENDING-1",
    }
    assert txn.manual_reconciliation_status == "reconciled"
    assert txn.reclassification_journal_entry == "JV-QR-MBQR-PENDING-1"
    assert txn.reconciled_by == "manager@example.com"
    assert txn.reconciled_at == datetime(2026, 3, 13, 18, 5, 0)
    assert txn.reconciliation_note == "Matched Maybank settlement report"
    assert txn.reconciliation_failed_reason is None
    assert comment.reference_doctype == "Maybank QR Transaction"
    assert comment.reference_name == "MBQR-PENDING-1"
    assert audit["action"] == "reconciled"
    assert audit["manager_id"] == "manager@example.com"
    assert audit["transaction_refno"] == "TXN-PENDING-1"
    assert audit["reclassification_journal_entry"] == "JV-QR-MBQR-PENDING-1"
    assert reconciliation_module.env.accounting_posts == ["MBQR-PENDING-1"]
    assert reconciliation_module.env.payment_updates == [
        ("FB-PAY-1", {"settlement_status": "reconciled"})
    ]


def test_mark_failed_updates_status_reason_and_audit(reconciliation_module):
    result = reconciliation_module.module.mark_manual_qr_reconciliation_failed(
        transaction_refno="TXN-PENDING-2",
        amount_sen="2500",
        business_date="2026-03-13",
        device_id="DEVICE-B",
        provider="maybank_qr",
        manager_id="manager@example.com",
        reason="no_bank_transaction",
        note="Not present in settlement report",
    )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-2"]
    audit = json.loads(reconciliation_module.env.comments[0].content)

    assert result == {
        "status": "ok",
        "manual_reconciliation_status": "reconciliation_failed",
        "failure_journal_entry": "JV-QR-FAIL-MBQR-PENDING-2",
    }
    assert txn.manual_reconciliation_status == "reconciliation_failed"
    assert txn.fb_order == "FB-ORDER-2"
    assert txn.sales_invoice == "SINV-2"
    assert txn.reconciliation_failed_reason == "no_bank_transaction"
    assert txn.reconciled_by == "manager@example.com"
    assert audit["action"] == "reconciliation_failed"
    assert audit["reason"] == "no_bank_transaction"
    assert audit["failure_journal_entry"] == "JV-QR-FAIL-MBQR-PENDING-2"
    assert reconciliation_module.env.payment_updates == [
        ("FB-PAY-2", {"settlement_status": "reconciliation_failed"})
    ]
    assert reconciliation_module.env.accounting_posts == []
    assert reconciliation_module.env.failure_accounting_posts == [
        ("MBQR-PENDING-2", "no_bank_transaction")
    ]


def test_provider_paid_truth_cannot_be_marked_reconciliation_failed(
    reconciliation_module,
):
    txn = reconciliation_module.env.transactions["MBQR-PENDING-2"]
    txn.status = "paid"

    with pytest.raises(
        reconciliation_module.frappe.ValidationError,
        match="Provider-paid Maybank QR truth cannot be marked",
    ):
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(
            transaction_refno="TXN-PENDING-2",
            amount_sen="2500",
            business_date="2026-03-13",
            device_id="DEVICE-B",
            provider="maybank_qr",
            manager_id="manager@example.com",
            reason="no_bank_transaction",
            note="A stale settlement file must not override provider truth",
        )

    assert txn.status == "paid"
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert txn.reclassification_journal_entry is None
    assert reconciliation_module.env.accounting_posts == []
    assert reconciliation_module.env.failure_accounting_posts == []
    assert reconciliation_module.env.payment_updates == []
    assert reconciliation_module.env.comments == []


def test_accounting_failure_does_not_complete_reconciliation(
    reconciliation_module, monkeypatch
):
    def fail_accounting(_source: Any) -> dict[str, Any]:
        raise reconciliation_module.frappe.ValidationError(
            "submitted suspense GL evidence is missing"
        )

    monkeypatch.setattr(
        reconciliation_module.module,
        "ensure_qr_suspense_reclassification",
        fail_accounting,
    )

    with pytest.raises(
        reconciliation_module.frappe.ValidationError,
        match="submitted suspense GL evidence is missing",
    ):
        reconciliation_module.module.mark_manual_qr_reconciled(
            transaction_refno="TXN-PENDING-1",
            amount_sen="1200",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            manager_id="manager@example.com",
            note="Cannot reconcile without posted GL evidence",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert txn.reclassification_journal_entry is None
    assert reconciliation_module.env.payment_updates == []
    assert reconciliation_module.env.comments == []


def test_failure_accounting_error_keeps_reconciliation_pending(
    reconciliation_module, monkeypatch
):
    def fail_accounting(_source: Any, _reason: str) -> dict[str, Any]:
        raise reconciliation_module.frappe.ValidationError(
            "company QR failure variance account is not configured"
        )

    monkeypatch.setattr(
        reconciliation_module.module,
        "ensure_qr_suspense_failure_reclassification",
        fail_accounting,
    )

    with pytest.raises(
        reconciliation_module.frappe.ValidationError,
        match="failure variance account is not configured",
    ):
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(
            transaction_refno="TXN-PENDING-2",
            amount_sen="2500",
            business_date="2026-03-13",
            device_id="DEVICE-B",
            provider="maybank_qr",
            manager_id="manager@example.com",
            reason="no_bank_transaction",
            note="Cannot fail without compensating accounting",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-2"]
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert txn.failure_journal_entry is None
    assert reconciliation_module.env.payment_updates == []
    assert reconciliation_module.env.comments == []


def test_exact_failure_retry_reproves_same_journal_without_duplicate_side_effects(
    reconciliation_module,
):
    payload = {
        "transaction_refno": "TXN-PENDING-2",
        "amount_sen": "2500",
        "business_date": "2026-03-13",
        "device_id": "DEVICE-B",
        "provider": "maybank_qr",
        "manager_id": "manager@example.com",
        "reason": "no_bank_transaction",
        "note": "Not present in settlement report",
    }

    first = reconciliation_module.module.mark_manual_qr_reconciliation_failed(**payload)
    second = reconciliation_module.module.mark_manual_qr_reconciliation_failed(**payload)

    assert first == second == {
        "status": "ok",
        "manual_reconciliation_status": "reconciliation_failed",
        "failure_journal_entry": "JV-QR-FAIL-MBQR-PENDING-2",
    }
    assert reconciliation_module.env.failure_accounting_posts == [
        ("MBQR-PENDING-2", "no_bank_transaction")
    ]
    assert reconciliation_module.env.failure_accounting_assertions == [
        ("MBQR-PENDING-2", "no_bank_transaction")
    ]
    assert reconciliation_module.env.payment_updates == [
        ("FB-PAY-2", {"settlement_status": "reconciliation_failed"})
    ]
    assert len(reconciliation_module.env.comments) == 1


def test_failure_retry_with_changed_note_is_rejected(
    reconciliation_module,
):
    payload = {
        "transaction_refno": "TXN-PENDING-2",
        "amount_sen": "2500",
        "business_date": "2026-03-13",
        "device_id": "DEVICE-B",
        "provider": "maybank_qr",
        "manager_id": "manager@example.com",
        "reason": "no_bank_transaction",
        "note": "Original reviewed note",
    }
    reconciliation_module.module.mark_manual_qr_reconciliation_failed(**payload)

    with pytest.raises(
        reconciliation_module.frappe.ValidationError,
        match="does not match this retry",
    ):
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(
            **{**payload, "note": "Changed note"}
        )

    assert reconciliation_module.env.failure_accounting_assertions == []
    assert len(reconciliation_module.env.comments) == 1


def test_mark_failed_requires_valid_reason(reconciliation_module):
    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(
            transaction_refno="TXN-PENDING-1",
            amount_sen="1200",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            manager_id="manager@example.com",
            reason="unsupported_reason",
            note="Invalid reason should fail",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert "reconciliation_failed_reason is invalid" in str(excinfo.value)
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert reconciliation_module.env.comments == []


def test_non_manager_role_cannot_reconcile(reconciliation_module, monkeypatch):
    monkeypatch.setattr(
        reconciliation_module.frappe,
        "session",
        SimpleNamespace(user="cashier@example.com"),
        raising=False,
    )
    monkeypatch.setattr(
        reconciliation_module.frappe,
        "get_roles",
        lambda user=None: ["POS User"] if user == "cashier@example.com" else [],
        raising=False,
    )

    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciled(
            transaction_refno="TXN-PENDING-1",
            amount_sen="1200",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            manager_id="cashier@example.com",
            note="Cashier should not reconcile",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert "Only a System Manager" in str(excinfo.value)


def test_reconciliation_validation_failure_rolls_back(
    reconciliation_module, monkeypatch
):
    rollbacks: list[bool] = []
    monkeypatch.setattr(
        reconciliation_module.frappe.db,
        "rollback",
        lambda: rollbacks.append(True),
        raising=False,
    )

    with pytest.raises(reconciliation_module.frappe.ValidationError):
        reconciliation_module.module.mark_manual_qr_reconciled(
            transaction_refno="TXN-PENDING-1",
            amount_sen="999",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            manager_id="manager@example.com",
            note="Wrong amount",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert rollbacks == [True]
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert reconciliation_module.env.updates == []
    assert reconciliation_module.env.comments == []


def test_manager_id_must_match_authenticated_user(reconciliation_module):
    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciled(
            transaction_refno="TXN-PENDING-1",
            amount_sen="1200",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            manager_id="another-manager@example.com",
            note="Spoofed manager identity should fail",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert "manager_id must match" in str(excinfo.value)
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert reconciliation_module.env.updates == []
    assert reconciliation_module.env.comments == []


def test_terminal_status_cannot_be_overwritten_without_audit(reconciliation_module):
    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(
            transaction_refno="TXN-RECONCILED",
            amount_sen="1200",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            manager_id="manager@example.com",
            reason="amount_mismatch",
            note="Attempt to overwrite terminal status",
        )

    txn = reconciliation_module.env.transactions["MBQR-RECONCILED"]
    assert "not pending manual reconciliation" in str(excinfo.value)
    assert txn.manual_reconciliation_status == "reconciled"
    assert txn.reconciled_by == "manager-a"
    assert reconciliation_module.env.updates == []
    assert reconciliation_module.env.comments == []


def test_manager_identity_is_required(reconciliation_module):
    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciled(
            transaction_refno="TXN-PENDING-1",
            amount_sen="1200",
            business_date="2026-03-13",
            device_id="DEVICE-A",
            provider="maybank_qr",
            note="Missing manager identity should fail",
        )

    assert "manager_id is required" in str(excinfo.value)
    assert reconciliation_module.env.comments == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"amount_sen": "1199"}, "amount_sen does not match"),
        ({"amount_sen": "1200.9"}, "amount_sen must be an integer"),
        ({"business_date": "2026-03-14"}, "business_date does not match"),
        ({"device_id": "DEVICE-B"}, "device_id does not match"),
        ({"provider": "other_provider"}, "provider does not match"),
    ],
)
def test_mark_reconciled_rejects_bank_matching_mismatches(
    reconciliation_module, override, message
):
    payload = {
        "transaction_refno": "TXN-PENDING-1",
        "amount_sen": "1200",
        "business_date": "2026-03-13",
        "device_id": "DEVICE-A",
        "provider": "maybank_qr",
        "manager_id": "manager@example.com",
        "note": "Mismatch should fail",
    }
    payload.update(override)

    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciled(**payload)

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert message in str(excinfo.value)
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert reconciliation_module.env.updates == []
    assert reconciliation_module.env.comments == []


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"amount_sen": "2499"}, "amount_sen does not match"),
        ({"business_date": "2026-03-14"}, "business_date does not match"),
        ({"device_id": "DEVICE-A"}, "device_id does not match"),
        ({"provider": "other_provider"}, "provider does not match"),
    ],
)
def test_mark_failed_rejects_bank_matching_mismatches(
    reconciliation_module, override, message
):
    payload = {
        "transaction_refno": "TXN-PENDING-2",
        "amount_sen": "2500",
        "business_date": "2026-03-13",
        "device_id": "DEVICE-B",
        "provider": "maybank_qr",
        "manager_id": "manager@example.com",
        "reason": "amount_mismatch",
        "note": "Mismatch should fail",
    }
    payload.update(override)

    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(**payload)

    txn = reconciliation_module.env.transactions["MBQR-PENDING-2"]
    assert message in str(excinfo.value)
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert reconciliation_module.env.updates == []
    assert reconciliation_module.env.comments == []


def build_transaction(
    *,
    name: str,
    transaction_refno: str,
    device_id: str,
    amount_sen: int,
    manual_reconciliation_status: str = "",
    receipt_file: str | None = None,
    receipt_uploaded_at: datetime | None = None,
    receipt_idempotency_key: str | None = None,
    receipt_payment_id: str | None = None,
    receipt_order_id: str | None = None,
    fb_order: str | None = None,
    fb_order_payment: str | None = None,
    sales_invoice: str | None = None,
    reconciled_by: str | None = None,
    reconciliation_failed_reason: str | None = None,
    provider_status: str = "pending",
) -> SimpleNamespace:
    return SimpleNamespace(
        doctype="Maybank QR Transaction",
        name=name,
        status=provider_status,
        transaction_refno=transaction_refno,
        device_id=device_id,
        amount_sen=amount_sen,
        sale_amount_sen=amount_sen,
        business_date="2026-03-13",
        provider="maybank_qr",
        created_at=datetime(2026, 3, 13, 18, 4, 30),
        manual_reconciliation_status=manual_reconciliation_status,
        receipt_file=receipt_file,
        receipt_uploaded_at=receipt_uploaded_at,
        receipt_idempotency_key=receipt_idempotency_key,
        receipt_payment_id=receipt_payment_id,
        receipt_order_id=receipt_order_id,
        receipt_amount_sen=amount_sen if receipt_payment_id else None,
        fb_order=fb_order,
        fb_order_payment=fb_order_payment,
        sales_invoice=sales_invoice,
        company=None,
        currency="MYR",
        suspense_account=None,
        idempotency_key=f"qr-key-{transaction_refno}",
        reconciliation_idempotency_key=f"reconcile-key-{transaction_refno}",
        reclassification_journal_entry=None,
        failure_journal_entry=None,
        failure_accounting_key=None,
        failure_variance_account=None,
        failure_cost_center=None,
        failure_accounting_reason=None,
        reconciled_by=reconciled_by,
        reconciled_at=None,
        reconciliation_note=None,
        reconciliation_failed_reason=reconciliation_failed_reason,
    )


def build_manual_reconciliation() -> SimpleNamespace:
    return SimpleNamespace(
        doctype="Manual QR Reconciliation",
        name="MQR-1",
        provider_session_id="static-session-1",
        device_id="DEVICE-A",
        staff_id="staff@example.com",
        amount_sen=1200,
        business_date="2026-03-13",
        company="KoPOS Cafe",
        currency="MYR",
        status="pending_reconciliation",
        created_at=datetime(2026, 3, 13, 18, 5, 0),
        evidence_json="{}",
        receipt_file=None,
        receipt_uploaded_at=None,
        receipt_idempotency_key=None,
        receipt_payment_id="PAY-STATIC",
        receipt_order_id="ORDER-STATIC",
        receipt_amount_sen=1200,
        fb_order_payment="FB-PAY-STATIC",
        fb_order="FB-STATIC-1",
        sales_invoice="SINV-STATIC-1",
        reconciliation_idempotency_key="manual-reconciliation-1",
        suspense_account="Manual QR Suspense - KPC",
        reclassification_journal_entry=None,
        failure_journal_entry=None,
        failure_accounting_key=None,
        failure_variance_account=None,
        failure_cost_center=None,
        failure_accounting_reason=None,
        reconciled_by=None,
        reconciled_at=None,
        reconciliation_note=None,
        reconciliation_failed_reason=None,
    )


class FakeCommentDoc:
    def __init__(self, env: ReconciliationEnv, payload: dict[str, Any]) -> None:
        self.env = env
        self.payload = payload

    def insert(self, ignore_permissions: bool = False) -> None:
        assert ignore_permissions is True
        self.env.comments.append(SimpleNamespace(**self.payload))
