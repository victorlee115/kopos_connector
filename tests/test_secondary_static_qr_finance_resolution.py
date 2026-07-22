from __future__ import annotations

import importlib
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.accounting import (  # noqa: E402
    secondary_static_claim_resolution as service,
)
from kopos_connector.kopos.doctype.manual_qr_reconciliation.manual_qr_reconciliation import (  # noqa: E402
    ManualQRReconciliation,
)


class Doc(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)


def _claim(**overrides: Any) -> Doc:
    values: dict[str, Any] = {
        "doctype": "Manual QR Reconciliation",
        "name": "MQR-SECONDARY-1",
        "status": "pending_reconciliation",
        "finance_resolution_status": "pending_review",
        "finance_resolution_decision": None,
        "finance_resolution_key": None,
        "finance_resolution_idempotency_key": None,
        "finance_reviewed_through_date": None,
        "finance_credit_reference": None,
        "finance_credit_date": None,
        "finance_credit_evidence_reference": None,
        "finance_credit_evidence_file": None,
        "finance_credit_evidence_sha256": None,
        "finance_credit_evidence_byte_length": None,
        "finance_clearing_account": None,
        "finance_liability_account": None,
        "finance_liability_journal_entry": None,
        "finance_resolution_note": None,
        "finance_refund_key": None,
        "finance_refund_idempotency_key": None,
        "finance_refund_reference": None,
        "finance_refund_date": None,
        "finance_refund_evidence_reference": None,
        "finance_refund_evidence_file": None,
        "finance_refund_evidence_sha256": None,
        "finance_refund_evidence_byte_length": None,
        "finance_refund_journal_entry": None,
        "finance_refund_note": None,
        "finance_resolved_by": None,
        "finance_resolved_at": None,
        "reconciliation_failed_reason": None,
        "reconciled_by": None,
        "reconciled_at": None,
        "reconciliation_note": None,
    }
    values.update(overrides)
    return Doc(**values)


def _identity() -> dict[str, Any]:
    return {
        "claim_name": "MQR-SECONDARY-1",
        "order_name": "FB-ORDER-1",
        "invoice_name": "SINV-1",
        "payment_row_name": "FBPAY-1",
        "winning_transaction": "MBQR-WINNER",
        "winning_transaction_refno": "MAYBANK-WINNER-REF-1",
        "company": "KoPOS Sdn Bhd",
        "currency": "MYR",
        "device_id": "TAB-A001",
        "amount_sen": 1250,
        "business_date": "2026-07-23",
    }


def _evidence(prefix: str) -> dict[str, Any]:
    return {
        "reference": f"{prefix}-EVIDENCE-1",
        "file": f"FILE-{prefix}-1",
        "sha256": "a" * 64,
        "byte_length": 128,
    }


def _controller(**values: Any) -> ManualQRReconciliation:
    document = ManualQRReconciliation()
    setattr(document, "get", lambda fieldname: values.get(fieldname))
    return document


def test_secondary_claim_controller_rejects_incomplete_terminal_states() -> None:
    invalid_states = (
        {
            "claim_role": "secondary_possible_duplicate",
            "winning_maybank_qr_transaction": "MBQR-WINNER",
            "status": "reconciliation_failed",
            "finance_resolution_status": "no_second_credit",
            "finance_resolution_decision": "no_second_credit",
        },
        {
            "claim_role": "secondary_possible_duplicate",
            "winning_maybank_qr_transaction": "MBQR-WINNER",
            "status": "pending_reconciliation",
            "finance_resolution_status": "refund_required",
            "finance_resolution_decision": "independent_second_credit",
            "finance_resolution_key": "liability-key",
        },
        {
            "claim_role": "secondary_possible_duplicate",
            "winning_maybank_qr_transaction": "MBQR-WINNER",
            "status": "reconciled",
            "finance_resolution_status": "refunded",
            "finance_resolution_decision": "independent_second_credit",
            "finance_liability_journal_entry": "JV-LIABILITY",
        },
    )
    for values in invalid_states:
        with pytest.raises(service.frappe.ValidationError):
            _controller(**values).validate()


def test_secondary_claim_controller_accepts_pending_finance_review() -> None:
    _controller(
        claim_role="secondary_possible_duplicate",
        winning_maybank_qr_transaction="MBQR-WINNER",
        status="pending_reconciliation",
        finance_resolution_status="pending_review",
    ).validate()


def _install_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[str]]:
    writes: list[dict[str, Any]] = []
    audits: list[str] = []

    def set_source(claim: Doc, values: dict[str, Any]) -> None:
        writes.append(dict(values))
        for fieldname, value in values.items():
            setattr(claim, fieldname, value)

    monkeypatch.setattr(service, "_set_source_values", set_source)
    monkeypatch.setattr(
        service,
        "_write_audit_comment",
        lambda claim, *, action, request: audits.append(action),
    )
    monkeypatch.setattr(
        service.frappe,
        "get_doc",
        lambda *args, **kwargs: Doc(name=str(args[-1]) if args else "DOC"),
    )
    service.frappe.session.user = "finance@example.test"
    return writes, audits


def test_no_second_credit_closes_claim_without_accounting_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    writes, audits = _install_mutations(monkeypatch)
    request = {
        "idempotency_key": "finance-no-credit-1",
        "reviewed_through_date": "2026-07-23",
        "note": "Bank records prove no independent second static QR credit.",
        "evidence": _evidence("NO-CREDIT"),
    }
    monkeypatch.setattr(
        service,
        "assert_secondary_static_claim_terminal",
        lambda *args, **kwargs: {"evidence_byte_length": 128},
    )

    result = service._confirm_no_second_credit(
        claim,
        identity=_identity(),
        request=request,
    )

    assert result["status"] == "resolved"
    assert claim.finance_resolution_status == "no_second_credit"
    assert claim.status == "reconciliation_failed"
    assert claim.reconciliation_failed_reason == "no_bank_transaction"
    assert claim.finance_liability_journal_entry is None
    assert claim.finance_refund_journal_entry is None
    assert claim.finance_resolved_by == "finance@example.test"
    assert audits == [service.ACTION_NO_SECOND_CREDIT]
    assert len(writes) == 1


def test_second_credit_posts_one_liability_and_exact_replay_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim()
    _writes, audits = _install_mutations(monkeypatch)
    context = {
        "journal_key": "liability-key-1",
        "clearing_account": "QR Clearing - K",
        "liability_account": "Customer Liability - K",
    }
    request = {
        "idempotency_key": "finance-credit-1",
        "credit_reference": "STATIC-CREDIT-1",
        "credit_date": "2026-07-23",
        "note": "Bank records prove one independent second static QR credit.",
        "evidence": _evidence("CREDIT"),
    }
    created: list[str] = []
    monkeypatch.setattr(service, "_liability_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        service,
        "_find_existing_journal",
        lambda source, key, *, link_field: getattr(source, link_field, None),
    )
    monkeypatch.setattr(
        service,
        "_create_or_recover_journal",
        lambda _context: created.append("JV-LIABILITY-1") or "JV-LIABILITY-1",
    )
    monkeypatch.setattr(
        service,
        "_validate_journal",
        lambda _context, name: {"journal_entry": name},
    )
    monkeypatch.setattr(
        service,
        "_assert_liability",
        lambda source, *, identity: {
            "journal_entry": source.finance_liability_journal_entry
        },
    )

    first = service._confirm_second_credit(claim, identity=_identity(), request=request)
    second = service._confirm_second_credit(claim, identity=_identity(), request=request)

    assert first["status"] == "refund_required"
    assert second["status"] == "already_recorded"
    assert claim.finance_resolution_status == "refund_required"
    assert claim.finance_liability_journal_entry == "JV-LIABILITY-1"
    assert claim.status == "pending_reconciliation"
    assert created == ["JV-LIABILITY-1"]
    assert audits == [service.ACTION_SECOND_CREDIT]


def test_refund_posts_inverse_entry_once_and_terminal_replay_reproves_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(
        finance_resolution_status="refund_required",
        finance_resolution_decision="independent_second_credit",
        finance_resolution_key="liability-key-1",
        finance_resolution_idempotency_key="finance-credit-1",
        finance_credit_reference="STATIC-CREDIT-1",
        finance_credit_date="2026-07-23",
        finance_credit_evidence_reference="CREDIT-EVIDENCE-1",
        finance_credit_evidence_file="FILE-CREDIT-1",
        finance_credit_evidence_sha256="a" * 64,
        finance_credit_evidence_byte_length=128,
        finance_clearing_account="QR Clearing - K",
        finance_liability_account="Customer Liability - K",
        finance_liability_journal_entry="JV-LIABILITY-1",
        finance_resolution_note="Independent credit was proven.",
    )
    _writes, audits = _install_mutations(monkeypatch)
    request = {
        "idempotency_key": "finance-refund-1",
        "refund_reference": "STATIC-REFUND-1",
        "refund_date": "2026-07-23",
        "note": "The independent second static QR credit was refunded in full.",
        "evidence": _evidence("REFUND"),
    }
    context = {"journal_key": "refund-key-1"}
    created: list[str] = []
    monkeypatch.setattr(
        service,
        "_assert_liability",
        lambda *args, **kwargs: {"journal_entry": "JV-LIABILITY-1"},
    )
    monkeypatch.setattr(
        service,
        "_validated_date",
        lambda value, **kwargs: value,
    )
    monkeypatch.setattr(service, "_refund_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(
        service,
        "_find_existing_journal",
        lambda source, key, *, link_field: getattr(source, link_field, None),
    )
    monkeypatch.setattr(
        service,
        "_create_or_recover_journal",
        lambda _context: created.append("JV-REFUND-1") or "JV-REFUND-1",
    )
    monkeypatch.setattr(
        service,
        "_validate_journal",
        lambda _context, name: {"journal_entry": name},
    )
    monkeypatch.setattr(
        service,
        "assert_secondary_static_claim_terminal",
        lambda *args, **kwargs: {
            "liability_journal_entry": "JV-LIABILITY-1",
            "refund_journal_entry": "JV-REFUND-1",
        },
    )

    first = service._record_refund(claim, identity=_identity(), request=request)
    second = service._record_refund(claim, identity=_identity(), request=request)

    assert first["status"] == "refunded"
    assert second["status"] == "already_refunded"
    assert claim.finance_resolution_status == "refunded"
    assert claim.finance_refund_journal_entry == "JV-REFUND-1"
    assert claim.status == "reconciled"
    assert claim.finance_resolved_by == "finance@example.test"
    assert created == ["JV-REFUND-1"]
    assert audits == [service.ACTION_REFUND]


def test_changed_idempotency_or_evidence_is_rejected_on_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(
        finance_resolution_status="no_second_credit",
        finance_resolution_decision="no_second_credit",
        finance_resolution_key="stored-key",
        finance_resolution_idempotency_key="stored-idempotency",
        finance_reviewed_through_date="2026-07-23",
        finance_credit_evidence_reference="NO-CREDIT-EVIDENCE-1",
        finance_credit_evidence_file="FILE-NO-CREDIT-1",
        finance_credit_evidence_sha256="a" * 64,
        finance_credit_evidence_byte_length=128,
        finance_resolution_note="Bank records prove no second credit.",
        status="reconciliation_failed",
        reconciliation_failed_reason="no_bank_transaction",
    )
    _install_mutations(monkeypatch)
    request = {
        "idempotency_key": "changed-idempotency",
        "reviewed_through_date": "2026-07-23",
        "note": "Bank records prove no second credit.",
        "evidence": _evidence("NO-CREDIT"),
    }

    with pytest.raises(service.frappe.ValidationError):
        service._confirm_no_second_credit(claim, identity=_identity(), request=request)


def test_public_endpoint_is_post_only_and_rejects_device_system_manager() -> None:
    api = importlib.import_module("kopos_connector.api")
    source = inspect.getsource(api.resolve_secondary_static_qr_claim)
    module_source = inspect.getsource(api)

    assert '@frappe.whitelist(methods=["POST"])' in source
    assert "require_system_manager()" in source
    assert "KOPOS_DEVICE_API_ROLE in get_session_roles()" in source
    assert "resolve_secondary_static_qr_claim_payload(payload)" in source
    assert '"resolve_secondary_static_qr_claim"' in module_source
