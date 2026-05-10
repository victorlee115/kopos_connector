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
    comments: list[SimpleNamespace] = field(default_factory=list)
    updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


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
                sales_invoice="SINV-1",
            ),
            "MBQR-PENDING-2": build_transaction(
                name="MBQR-PENDING-2",
                transaction_refno="TXN-PENDING-2",
                device_id="DEVICE-B",
                amount_sen=2500,
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
        assert doctype == "Maybank QR Transaction"
        rows = []
        for txn in env.transactions.values():
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
        assert doctype == "Maybank QR Transaction"
        if isinstance(filters, dict):
            for txn in env.transactions.values():
                if all(getattr(txn, key, None) == value for key, value in filters.items()):
                    return getattr(txn, fieldname)
        return None

    def fake_set_value(doctype: str, name: str, values: dict[str, Any], **_kwargs: Any) -> None:
        assert doctype == "Maybank QR Transaction"
        txn = env.transactions[name]
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
        raise AssertionError(f"unexpected get_doc call: {args!r} {kwargs!r}")

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
    return SimpleNamespace(module=manual_qr_receipt, env=env, frappe=frappe)


def test_list_returns_only_pending_reconciliation_transactions(reconciliation_module):
    rows = reconciliation_module.module.list_pending_manual_qr_reconciliations()

    assert [row["transaction_refno"] for row in rows] == [
        "TXN-PENDING-1",
        "TXN-PENDING-2",
    ]
    assert rows[0]["manual_reconciliation_status"] == "pending_reconciliation"
    assert rows[0]["device_id"] == "DEVICE-A"
    assert rows[0]["sale_amount_sen"] == 1200
    assert rows[0]["receipt_file"] == "FILE-1"
    assert rows[0]["receipt_uploaded_at"] == datetime(2026, 3, 13, 18, 6, 0)
    assert rows[0]["receipt_idempotency_key"] == "receipt-key-1"
    assert rows[0]["receipt_payment_id"] == "PAY-1"
    assert rows[0]["receipt_order_id"] == "ORDER-1"
    assert rows[0]["fb_order"] == "FB-ORDER-1"
    assert rows[0]["sales_invoice"] == "SINV-1"
    assert "TXN-NORMAL" not in {row["transaction_refno"] for row in rows}


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


def test_fetch_manual_qr_reconciliation_status_accepts_transaction_refnos(reconciliation_module):
    result = reconciliation_module.module.fetch_manual_qr_reconciliation_status(
        transaction_refnos=["TXN-FAILED"]
    )

    assert result["statuses"][0]["transaction_refno"] == "TXN-FAILED"
    assert result["statuses"][0]["reconciliation_status"] == "reconciliation_failed"
    assert result["statuses"][0]["reconciliation_failed_reason"] == "amount_mismatch"


def test_mark_reconciled_updates_status_and_audit(reconciliation_module):
    result = reconciliation_module.module.mark_manual_qr_reconciled(
        transaction_refno="TXN-PENDING-1",
        manager_id="manager@example.com",
        note="Matched Maybank settlement report",
    )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    comment = reconciliation_module.env.comments[0]
    audit = json.loads(comment.content)

    assert result == {"status": "ok", "manual_reconciliation_status": "reconciled"}
    assert txn.manual_reconciliation_status == "reconciled"
    assert txn.reconciled_by == "manager@example.com"
    assert txn.reconciled_at == datetime(2026, 3, 13, 18, 5, 0)
    assert txn.reconciliation_note == "Matched Maybank settlement report"
    assert txn.reconciliation_failed_reason is None
    assert comment.reference_doctype == "Maybank QR Transaction"
    assert comment.reference_name == "MBQR-PENDING-1"
    assert audit["action"] == "reconciled"
    assert audit["manager_id"] == "manager@example.com"
    assert audit["transaction_refno"] == "TXN-PENDING-1"


def test_mark_failed_updates_status_reason_and_audit(reconciliation_module):
    result = reconciliation_module.module.mark_manual_qr_reconciliation_failed(
        transaction_refno="TXN-PENDING-2",
        manager_id="manager@example.com",
        reason="no_bank_transaction",
        note="Not present in settlement report",
    )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-2"]
    audit = json.loads(reconciliation_module.env.comments[0].content)

    assert result == {
        "status": "ok",
        "manual_reconciliation_status": "reconciliation_failed",
    }
    assert txn.manual_reconciliation_status == "reconciliation_failed"
    assert txn.reconciliation_failed_reason == "no_bank_transaction"
    assert txn.reconciled_by == "manager@example.com"
    assert audit["action"] == "reconciliation_failed"
    assert audit["reason"] == "no_bank_transaction"


def test_mark_failed_requires_valid_reason(reconciliation_module):
    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciliation_failed(
            transaction_refno="TXN-PENDING-1",
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
            manager_id="cashier@example.com",
            note="Cashier should not reconcile",
        )

    txn = reconciliation_module.env.transactions["MBQR-PENDING-1"]
    assert "Only a System Manager" in str(excinfo.value)
    assert txn.manual_reconciliation_status == "pending_reconciliation"
    assert reconciliation_module.env.updates == []
    assert reconciliation_module.env.comments == []


def test_manager_id_must_match_authenticated_user(reconciliation_module):
    with pytest.raises(reconciliation_module.frappe.ValidationError) as excinfo:
        reconciliation_module.module.mark_manual_qr_reconciled(
            transaction_refno="TXN-PENDING-1",
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
            note="Missing manager identity should fail",
        )

    assert "manager_id is required" in str(excinfo.value)
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
    sales_invoice: str | None = None,
    reconciled_by: str | None = None,
    reconciliation_failed_reason: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        transaction_refno=transaction_refno,
        device_id=device_id,
        sale_amount_sen=amount_sen,
        created_at=datetime(2026, 3, 13, 18, 4, 30),
        manual_reconciliation_status=manual_reconciliation_status,
        receipt_file=receipt_file,
        receipt_uploaded_at=receipt_uploaded_at,
        receipt_idempotency_key=receipt_idempotency_key,
        receipt_payment_id=receipt_payment_id,
        receipt_order_id=receipt_order_id,
        receipt_amount_sen=amount_sen if receipt_payment_id else None,
        fb_order=fb_order,
        sales_invoice=sales_invoice,
        idempotency_key=f"qr-key-{transaction_refno}",
        reconciled_by=reconciled_by,
        reconciled_at=None,
        reconciliation_note=None,
        reconciliation_failed_reason=reconciliation_failed_reason,
    )


class FakeCommentDoc:
    def __init__(self, env: ReconciliationEnv, payload: dict[str, Any]) -> None:
        self.env = env
        self.payload = payload

    def insert(self, ignore_permissions: bool = False) -> None:
        assert ignore_permissions is True
        self.env.comments.append(SimpleNamespace(**self.payload))
