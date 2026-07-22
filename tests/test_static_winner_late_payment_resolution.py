from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.kopos.services.accounting import (  # noqa: E402
    static_winner_late_payment as service,
)


class Doc(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)


def _identity() -> dict[str, Any]:
    return {
        "source_name": "MBQR-LATE",
        "order_name": "FB-ORDER-1",
        "invoice_name": "SINV-1",
        "payment_row_name": "FBPAY-1",
        "winning_static_reconciliation": "MQR-STATIC-1",
        "device_id": "TAB-A001",
        "company": "KoPOS Sdn Bhd",
        "currency": "MYR",
    }


def _order() -> Doc:
    return Doc(
        name="FB-ORDER-1",
        sales_invoice="SINV-1",
        automatic_qr_static_reconciliation="MQR-STATIC-1",
    )


def _transaction(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "doctype": "Maybank QR Transaction",
        "name": "MBQR-LATE",
        "fb_order_payment": "FBPAY-1",
        "duplicate_payment_status": None,
        "duplicate_winning_channel": None,
        "duplicate_winning_transaction": None,
        "duplicate_winning_static_reconciliation": None,
        "consumption_key": None,
        "sales_invoice": None,
        "invoice_consumption_key": None,
        "consumed_at": None,
        "duplicate_accounting_key": None,
        "duplicate_liability_journal_entry": None,
        "duplicate_refund_key": None,
        "duplicate_refund_journal_entry": None,
    }
    values.update(overrides)
    return values


def _reconciliation(status: str) -> Doc:
    return Doc(
        doctype="Manual QR Reconciliation",
        name="MQR-STATIC-1",
        status=status,
        claim_role="winning_settlement",
        winning_maybank_qr_transaction=None,
        fb_order="FB-ORDER-1",
        fb_order_payment="FBPAY-1",
        sales_invoice="SINV-1",
        device_id="TAB-A001",
        company="KoPOS Sdn Bhd",
        currency="MYR",
        reconciliation_failed_reason=(
            "no_bank_transaction" if status == "reconciliation_failed" else None
        ),
        failure_accounting_reason=(
            "no_bank_transaction" if status == "reconciliation_failed" else None
        ),
        failure_journal_entry=(
            "JV-FAIL-1" if status == "reconciliation_failed" else None
        ),
        reclassification_journal_entry=None,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reconciliation: Doc,
) -> list[tuple[Any, ...]]:
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(service, "_lock_static_reconciliation", lambda _order: reconciliation)
    monkeypatch.setattr(service, "_validate_duplicate_identity", lambda *args, **kwargs: _identity())

    def set_source(source: Any, values: dict[str, Any]) -> None:
        source.update(values)
        writes.append((source["name"], values))

    monkeypatch.setattr(service, "_set_source_values", set_source)
    monkeypatch.setattr(
        service.frappe.db,
        "set_value",
        lambda *args, **kwargs: writes.append(args),
    )
    return writes


def test_pending_static_reconciliation_keeps_late_payment_possible_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciliation = _reconciliation("pending_reconciliation")
    writes = _install(monkeypatch, reconciliation=reconciliation)
    transaction = _transaction()
    monkeypatch.setattr(
        service,
        "register_duplicate_paid_incident",
        lambda *args, **kwargs: pytest.fail("liability must not be posted while pending"),
    )

    result = service.resolve_late_paid_after_static_winner(
        transaction,
        order_doc=_order(),
        paid_attempts=[transaction],
    )

    assert result["duplicate_payment_status"] == "possible_duplicate"
    assert result["liability_journal_entry"] is None
    assert transaction["duplicate_payment_status"] == "possible_duplicate"
    assert transaction["consumption_key"] is None
    assert len(writes) == 1


def test_reconciled_static_payment_promotes_possible_duplicate_to_liability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciliation = _reconciliation("reconciled")
    _install(monkeypatch, reconciliation=reconciliation)
    transaction = _transaction(
        duplicate_payment_status="possible_duplicate",
        duplicate_winning_channel="static_qr",
        duplicate_winning_static_reconciliation="MQR-STATIC-1",
    )
    observed: list[str] = []

    def register(source: Any, **kwargs: Any) -> dict[str, Any]:
        observed.append(source["name"])
        assert kwargs["winning_transaction_name"] == ""
        return {
            "status": "payment_incident",
            "duplicate_payment_status": "refund_required",
            "sales_invoice_created": False,
        }

    monkeypatch.setattr(service, "register_duplicate_paid_incident", register)

    result = service.resolve_late_paid_after_static_winner(
        transaction,
        order_doc=_order(),
        paid_attempts=[transaction],
    )

    assert result["duplicate_payment_status"] == "refund_required"
    assert observed == ["MBQR-LATE"]


def test_failed_static_payment_consumes_earliest_maybank_into_existing_invoice_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciliation = _reconciliation("reconciliation_failed")
    writes = _install(monkeypatch, reconciliation=reconciliation)
    transaction = _transaction(duplicate_payment_status="possible_duplicate")
    monkeypatch.setattr(
        service,
        "assert_qr_suspense_failure_reclassification",
        lambda source, reason: {
            "journal_entry": "JV-FAIL-1",
            "failure_reason": reason,
        },
    )
    monkeypatch.setattr(
        service,
        "ensure_qr_suspense_reclassification",
        lambda source: {"journal_entry": "JV-RECOVER-1"},
    )

    result = service.resolve_late_paid_after_static_winner(
        transaction,
        order_doc=_order(),
        paid_attempts=[transaction],
    )

    assert result["status"] == "already_submitted"
    assert result["duplicate_payment_status"] == "settled_existing_sale"
    assert result["sales_invoice"] == "SINV-1"
    assert result["sales_invoice_created"] is False
    assert transaction["consumption_key"] == "FB-ORDER-1"
    assert transaction["sales_invoice"] == "SINV-1"
    assert transaction["invoice_consumption_key"] == "SINV-1"
    assert transaction["consumed_at"] is not None
    assert reconciliation.status == "reconciled"
    assert reconciliation.reclassification_journal_entry == "JV-RECOVER-1"
    assert not any(write[0] == "FB Order" for write in writes if isinstance(write, tuple))


def test_failed_static_uses_earliest_paid_attempt_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciliation = _reconciliation("reconciliation_failed")
    _install(monkeypatch, reconciliation=reconciliation)
    earlier = _transaction(name="MBQR-EARLY")
    later = _transaction(name="MBQR-LATE")

    result = service.resolve_late_paid_after_static_winner(
        later,
        order_doc=_order(),
        paid_attempts=[earlier, later],
    )

    assert result["duplicate_payment_status"] == "possible_duplicate"
    assert later["consumption_key"] is None


def test_settled_existing_sale_replay_reproves_recovery_without_new_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reconciliation = _reconciliation("reconciled")
    reconciliation.failure_accounting_reason = "no_bank_transaction"
    reconciliation.failure_journal_entry = "JV-FAIL-1"
    reconciliation.reclassification_journal_entry = "JV-RECOVER-1"
    _install(monkeypatch, reconciliation=reconciliation)
    transaction = _transaction(
        duplicate_payment_status="settled_existing_sale",
        duplicate_winning_channel="static_qr",
        duplicate_winning_static_reconciliation="MQR-STATIC-1",
        consumption_key="FB-ORDER-1",
        sales_invoice="SINV-1",
        invoice_consumption_key="SINV-1",
        consumed_at="2026-07-23 12:00:00",
    )
    assertions: list[str] = []
    monkeypatch.setattr(
        service,
        "assert_qr_suspense_reclassification",
        lambda source: assertions.append(source.name)
        or {"journal_entry": "JV-RECOVER-1"},
    )

    result = service.resolve_late_paid_after_static_winner(
        transaction,
        order_doc=_order(),
        paid_attempts=[transaction],
    )

    assert result["status"] == "already_submitted"
    assert result["duplicate_payment_status"] == "settled_existing_sale"
    assert assertions == ["MQR-STATIC-1"]
