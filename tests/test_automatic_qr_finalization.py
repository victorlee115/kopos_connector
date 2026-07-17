from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

service = importlib.import_module(
    "kopos_connector.kopos.services.accounting.automatic_qr_finalization_service"
)
core = importlib.import_module(
    "kopos_connector.kopos.services.accounting.automatic_qr_finalization_core"
)
recovery = importlib.import_module(
    "kopos_connector.kopos.services.accounting.automatic_qr_finalization_recovery"
)
ROOT = Path(__file__).resolve().parents[1]


class FakeDocument(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)


class FakeOrder(FakeDocument):
    save_count = 0
    submit_count = 0

    def save(self, *, ignore_permissions: bool) -> None:
        assert ignore_permissions is True
        self.save_count += 1

    def submit(self) -> None:
        self.submit_count += 1
        self.docstatus = 1
        self.invoice_status = "Posted"
        self.sales_invoice = "SINV-1"


def _payment(**overrides: Any) -> FakeDocument:
    values: dict[str, Any] = {
        "name": "FBPAY-1",
        "source_payment_id": "local-payment-1",
        "payment_method": "DuitNow QR",
        "payment_channel_code": "maybank",
        "amount": "12.50",
        "reference_no": None,
        "external_transaction_id": None,
        "maybank_qr_transaction": None,
        "is_manual_confirmation": 0,
        "settlement_status": "verified",
        "suspense_account": None,
        "manual_qr_reconciliation": None,
        "reconciliation_idempotency_key": None,
    }
    values.update(overrides)
    return FakeDocument(**values)


def _order(payment: FakeDocument, **overrides: Any) -> FakeOrder:
    values: dict[str, Any] = {
        "name": "FB-ORDER-1",
        "docstatus": 0,
        "accepted_sale_fingerprint": "a" * 64,
        "automatic_qr_payment": "FBPAY-1",
        "automatic_qr_state": "provider_pending",
        "device_id": "TAB-A001",
        "currency": "MYR",
        "company": "KoPOS Sdn Bhd",
        "payments": [payment],
        "invoice_status": None,
        "sales_invoice": None,
    }
    values.update(overrides)
    return FakeOrder(**values)


def _attempt(
    name: str = "MBQR-1",
    reference: str = "MBB-REF-1",
    **overrides: Any,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": name,
        "transaction_refno": reference,
        "status": "paid",
        "maybank_status": 1,
        "sale_amount_sen": 1250,
        "currency": "MYR",
        "provider": "maybank_qr",
        "device_id": "TAB-A001",
        "outlet_id": "OUTLET-1",
        "qr_data": "issued-provider-qr",
        "paid_at": "2026-03-13 12:00:00",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FBPAY-1",
        "sales_invoice": None,
        "consumption_key": None,
        "invoice_consumption_key": None,
        "reconciliation_idempotency_key": None,
        "company": "KoPOS Sdn Bhd",
        "suspense_account": None,
        "manual_reconciliation_status": None,
        "reconciliation_note": None,
    }
    values.update(overrides)
    return values


def _install_finalizer_state(
    monkeypatch: Any,
    *,
    order: FakeOrder,
    attempts: list[dict[str, Any]],
    requested_name: str,
) -> list[tuple[Any, ...]]:
    writes: list[tuple[Any, ...]] = []
    requested = next(attempt for attempt in attempts if attempt["name"] == requested_name)
    monkeypatch.setattr(
        service.frappe.db,
        "get_value",
        lambda *args, **kwargs: {
            "name": requested["name"],
            "fb_order": requested["fb_order"],
            "fb_order_payment": requested["fb_order_payment"],
        },
    )
    monkeypatch.setattr(service.frappe.db, "sql", lambda *args, **kwargs: [(order.name,)])
    monkeypatch.setattr(
        service.frappe.db,
        "savepoint",
        lambda _name: None,
        raising=False,
    )
    monkeypatch.setattr(
        service.frappe.db,
        "rollback",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(service.frappe, "get_doc", lambda *args, **kwargs: order)
    monkeypatch.setattr(
        core,
        "_load_paid_attempts_for_update",
        lambda *args, **kwargs: attempts,
    )
    monkeypatch.setattr(
        service.frappe.db,
        "set_value",
        lambda *args, **kwargs: writes.append(args),
    )
    monkeypatch.setattr(service.frappe, "log_error", lambda *args, **kwargs: None)
    return writes


def test_paid_transition_queues_after_commit_with_stable_job_identity(monkeypatch: Any) -> None:
    queued: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(
        service.frappe,
        "enqueue",
        lambda *args, **kwargs: queued.append((args, kwargs)),
        raising=False,
    )

    service.enqueue_automatic_qr_finalization("MBQR-1")
    service.enqueue_automatic_qr_finalization("MBQR-1")

    assert len(queued) == 2
    first_args, first_kwargs = queued[0]
    assert first_args == (
        "kopos_connector.kopos.services.accounting."
        "automatic_qr_finalization_service.finalize_paid_automatic_qr_sale",
    )
    assert first_kwargs["enqueue_after_commit"] is True
    assert first_kwargs["queue"] == "short"
    assert first_kwargs["transaction_name"] == "MBQR-1"
    assert first_kwargs["job_id"] == queued[1][1]["job_id"]


def test_provider_paid_draft_submits_stored_sale_once_and_replay_is_success(
    monkeypatch: Any,
) -> None:
    payment = _payment()
    order = _order(payment)
    attempt = _attempt()
    writes = _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
        requested_name="MBQR-1",
    )

    first = service.finalize_paid_automatic_qr_sale("MBQR-1")
    # FB Order.before_submit binds these fields in production. The fake order
    # models only the outer lifecycle, so reflect that durable bind explicitly.
    attempt["consumption_key"] = "FB-ORDER-1"
    attempt["sales_invoice"] = "SINV-1"
    attempt["invoice_consumption_key"] = "SINV-1"
    second = service.finalize_paid_automatic_qr_sale("MBQR-1")

    assert first["status"] == "submitted"
    assert second["status"] == "already_submitted"
    assert order.submit_count == 1
    assert payment.reference_no == "MBB-REF-1"
    assert payment.external_transaction_id == "MBB-REF-1"
    assert payment.maybank_qr_transaction == "MBQR-1"
    assert payment.settlement_status == "verified"
    assert payment.is_manual_confirmation == 0
    assert order.automatic_qr_state == "finalized"
    assert any(
        write[:4] == ("FB Order", "FB-ORDER-1", "automatic_qr_state", "finalized")
        for write in writes
    )


def test_hyphenated_maybank_channel_finalizes_with_shared_normalizer(
    monkeypatch: Any,
) -> None:
    payment = _payment(payment_channel_code="Maybank-QR")
    order = _order(payment)
    attempt = _attempt()
    _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
        requested_name="MBQR-1",
    )

    result = service.finalize_paid_automatic_qr_sale("MBQR-1")

    assert result["status"] == "submitted"
    assert order.submit_count == 1
    assert payment.maybank_qr_transaction == "MBQR-1"
    assert payment.settlement_status == "verified"


@pytest.mark.parametrize("company", [None, "Other Company"])
def test_provider_transaction_company_must_match_prepared_sale(
    monkeypatch: Any,
    company: str | None,
) -> None:
    payment = _payment()
    order = _order(payment)
    attempt = _attempt(company=company)
    _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
        requested_name="MBQR-1",
    )

    with pytest.raises(
        service.frappe.ValidationError,
        match="company does not match",
    ):
        service.finalize_paid_automatic_qr_sale("MBQR-1")

    assert order.submit_count == 0


def test_submitted_manual_pending_same_attempt_is_not_blocked_or_duplicated(
    monkeypatch: Any,
) -> None:
    payment = _payment(
        reference_no="MBB-REF-1",
        external_transaction_id="MBB-REF-1",
        maybank_qr_transaction="MBQR-1",
        is_manual_confirmation=1,
        settlement_status="pending_reconciliation",
    )
    order = _order(
        payment,
        docstatus=1,
        automatic_qr_state="finalized",
        invoice_status="Posted",
        sales_invoice="SINV-1",
    )
    attempt = _attempt(
        consumption_key="FB-ORDER-1",
        sales_invoice="SINV-1",
        manual_reconciliation_status="pending_reconciliation",
    )
    writes = _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
        requested_name="MBQR-1",
    )

    result = service.finalize_paid_automatic_qr_sale("MBQR-1")

    assert result["status"] == "already_submitted"
    assert order.submit_count == 0
    assert payment.settlement_status == "pending_reconciliation"
    assert not any(write[0] == "Maybank QR Transaction" for write in writes)


def test_provider_paid_overrides_prior_manual_failure_and_reclassifies_suspense(
    monkeypatch: Any,
) -> None:
    payment = _payment(
        reference_no="MBB-REF-1",
        external_transaction_id="MBB-REF-1",
        maybank_qr_transaction="MBQR-1",
        is_manual_confirmation=1,
        settlement_status="reconciliation_failed",
        suspense_account="QR Suspense - K",
        reconciliation_idempotency_key="reconcile-MBQR-1",
    )
    order = _order(
        payment,
        docstatus=1,
        automatic_qr_state="finalized",
        invoice_status="Posted",
        sales_invoice="SINV-1",
    )
    attempt = _attempt(
        consumption_key="FB-ORDER-1",
        sales_invoice="SINV-1",
        invoice_consumption_key="SINV-1",
        manual_reconciliation_status="reconciliation_failed",
        reconciliation_idempotency_key="reconcile-MBQR-1",
        company=None,
    )
    writes = _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
        requested_name="MBQR-1",
    )
    monkeypatch.setattr(
        core,
        "ensure_qr_suspense_reclassification",
        lambda source: {"journal_entry": "JV-QR-1"},
    )

    result = service.finalize_paid_automatic_qr_sale("MBQR-1")

    assert result["status"] == "already_submitted"
    assert result["settlement_status"] == "reconciled"
    assert result["reclassification_journal_entry"] == "JV-QR-1"
    assert order.submit_count == 0
    assert payment.settlement_status == "reconciled"
    transaction_updates = [
        write[2]
        for write in writes
        if write[0:2] == ("Maybank QR Transaction", "MBQR-1")
    ]
    assert transaction_updates[0]["manual_reconciliation_status"] == (
        "pending_reconciliation"
    )
    context_update = next(
        update for update in transaction_updates if "suspense_account" in update
    )
    assert context_update == {
        "company": "KoPOS Sdn Bhd",
        "suspense_account": "QR Suspense - K",
    }
    assert transaction_updates[-1]["manual_reconciliation_status"] == "reconciled"
    assert transaction_updates[-1]["reconciliation_failed_reason"] is None


def test_provider_paid_failure_override_stays_pending_when_accounting_is_delayed(
    monkeypatch: Any,
) -> None:
    payment = _payment(
        reference_no="MBB-REF-1",
        external_transaction_id="MBB-REF-1",
        maybank_qr_transaction="MBQR-1",
        is_manual_confirmation=1,
        settlement_status="reconciliation_failed",
        suspense_account="QR Suspense - K",
        reconciliation_idempotency_key="reconcile-MBQR-1",
    )
    order = _order(
        payment,
        docstatus=1,
        automatic_qr_state="finalized",
        invoice_status="Failed",
        sales_invoice=None,
    )
    attempt = _attempt(
        consumption_key="FB-ORDER-1",
        manual_reconciliation_status="reconciliation_failed",
        reconciliation_idempotency_key="reconcile-MBQR-1",
    )
    writes = _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
        requested_name="MBQR-1",
    )
    monkeypatch.setattr(
        core,
        "ensure_qr_suspense_reclassification",
        lambda source: (_ for _ in ()).throw(RuntimeError("invoice projection pending")),
    )
    monkeypatch.setattr(core, "log_sanitized_error", lambda *args, **kwargs: None)

    result = service.finalize_paid_automatic_qr_sale("MBQR-1")

    assert result["status"] == "already_submitted"
    assert result["settlement_status"] == "pending_reconciliation"
    assert order.submit_count == 0
    assert payment.settlement_status == "pending_reconciliation"
    source_override = next(
        write
        for write in writes
        if write[0:2] == ("Maybank QR Transaction", "MBQR-1")
    )
    assert source_override[2]["manual_reconciliation_status"] == (
        "pending_reconciliation"
    )
    assert source_override[2]["reconciliation_failed_reason"] is None


def test_later_paid_attempt_becomes_incident_without_second_invoice(
    monkeypatch: Any,
) -> None:
    payment = _payment(
        reference_no="MBB-WINNER",
        external_transaction_id="MBB-WINNER",
        maybank_qr_transaction="MBQR-WINNER",
    )
    order = _order(
        payment,
        docstatus=1,
        automatic_qr_state="finalized",
        invoice_status="Posted",
        sales_invoice="SINV-1",
    )
    later = _attempt("MBQR-LATE", "MBB-LATE")
    writes = _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[later],
        requested_name="MBQR-LATE",
    )

    result = service.finalize_paid_automatic_qr_sale("MBQR-LATE")

    assert result["status"] == "payment_incident"
    assert result["transaction"] == "MBQR-LATE"
    assert result["fb_order"] == "FB-ORDER-1"
    assert result["winning_transaction"] == "MBQR-WINNER"
    assert result["settlement_status"] == "accounting_pending"
    assert result["duplicate_payment_status"] == "accounting_pending"
    assert result["liability_journal_entry"] is None
    assert result["sales_invoice_created"] is False
    assert order.submit_count == 0
    incident_write = next(
        write for write in writes if write[0] == "Maybank QR Transaction"
    )
    assert incident_write[1] == "MBQR-LATE"
    assert incident_write[2]["duplicate_payment_status"] == "accounting_pending"
    assert incident_write[2]["duplicate_winning_transaction"] == "MBQR-WINNER"
    assert "manual_reconciliation_status" not in incident_write[2]


def test_earliest_provider_paid_attempt_wins_even_if_later_job_runs_first(
    monkeypatch: Any,
) -> None:
    payment = _payment()
    order = _order(payment)
    earlier = _attempt("MBQR-EARLY", "MBB-EARLY")
    later = _attempt("MBQR-LATE", "MBB-LATE")
    writes = _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[earlier, later],
        requested_name="MBQR-LATE",
    )

    result = service.finalize_paid_automatic_qr_sale("MBQR-LATE")

    assert result["status"] == "payment_incident"
    assert result["winning_transaction"] == "MBQR-EARLY"
    assert result["duplicate_payment_status"] == "accounting_pending"
    assert order.submit_count == 0
    assert any(
        write[0:2] == ("Maybank QR Transaction", "MBQR-LATE")
        for write in writes
    )


def test_winning_sale_commits_before_duplicate_incident_bookkeeping_failure(
    monkeypatch: Any,
) -> None:
    payment = _payment()
    order = _order(payment)
    winner = _attempt("MBQR-WINNER", "MBB-WINNER")
    later = _attempt("MBQR-LATE", "MBB-LATE")
    events: list[str] = []
    _install_finalizer_state(
        monkeypatch,
        order=order,
        attempts=[winner, later],
        requested_name="MBQR-WINNER",
    )
    monkeypatch.setattr(
        core,
        "_reload_late_paid_incident_for_update",
        lambda _order_name, _transaction_name: (order, later),
    )

    def set_value(doctype: str, *args: Any, **kwargs: Any) -> None:
        if doctype == "Maybank QR Transaction":
            events.append("incident_write_failed")
            raise RuntimeError("incident storage unavailable")
        events.append("sale_finalized")

    monkeypatch.setattr(service.frappe.db, "set_value", set_value)
    monkeypatch.setattr(
        service.frappe.db,
        "commit",
        lambda: events.append("commit"),
    )
    monkeypatch.setattr(
        service.frappe.db,
        "rollback",
        lambda: events.append("rollback"),
    )
    monkeypatch.setattr(core, "log_sanitized_error", lambda *args: None)

    result = service.finalize_paid_automatic_qr_sale("MBQR-WINNER")

    assert result["status"] == "submitted"
    assert result["incident_registration_pending"] == ["MBQR-LATE"]
    assert order.docstatus == 1
    assert order.submit_count == 1
    assert events.index("commit") < events.index("incident_write_failed")
    assert events[-1] == "rollback"


def test_post_commit_duplicate_registration_reloads_refunded_row_before_write(
    monkeypatch: Any,
) -> None:
    payment = _payment(
        maybank_qr_transaction="MBQR-WINNER",
        reference_no="MBB-WINNER",
        external_transaction_id="MBB-WINNER",
    )
    order = _order(
        payment,
        docstatus=1,
        sales_invoice="SINV-1",
        automatic_qr_state="finalized",
    )
    stale = _attempt("MBQR-LATE", "MBB-LATE", duplicate_payment_status=None)
    fresh = _attempt(
        "MBQR-LATE",
        "MBB-LATE",
        duplicate_payment_status="refunded",
        duplicate_refund_journal_entry="JV-REFUND-1",
    )
    seen: list[Any] = []
    commits: list[None] = []
    monkeypatch.setattr(service.frappe.db, "commit", lambda: commits.append(None))
    monkeypatch.setattr(
        core,
        "_reload_late_paid_incident_for_update",
        lambda order_name, transaction_name: (
            order,
            fresh,
        )
        if (order_name, transaction_name) == ("FB-ORDER-1", "MBQR-LATE")
        else (_ for _ in ()).throw(AssertionError("unexpected incident identity")),
    )
    monkeypatch.setattr(
        core,
        "_mark_late_paid_incident",
        lambda transaction, **_kwargs: seen.append(transaction)
        or {"duplicate_payment_status": transaction["duplicate_payment_status"]},
    )

    pending = service._register_late_paid_incidents_after_sale_commit(
        [stale],
        order_doc=order,
        winning_transaction_name="MBQR-WINNER",
    )

    assert pending == []
    assert seen == [fresh]
    assert seen[0]["duplicate_payment_status"] == "refunded"
    assert len(commits) == 2


def test_recovery_sweep_commits_each_candidate_and_continues_after_failure(
    monkeypatch: Any,
) -> None:
    captured_sql: list[str] = []
    commits: list[None] = []
    rollbacks: list[None] = []

    def sql(query: str, *args: Any, **kwargs: Any) -> list[dict[str, str]]:
        captured_sql.append(query)
        return [{"name": "MBQR-1"}, {"name": "MBQR-2"}]

    def finalize(name: str) -> dict[str, Any]:
        if name == "MBQR-2":
            raise RuntimeError("worker unavailable")
        return {"status": "submitted", "transaction": name}

    monkeypatch.setattr(service.frappe.db, "sql", sql)
    monkeypatch.setattr(recovery, "finalize_paid_automatic_qr_sale", finalize)
    monkeypatch.setattr(service.frappe.db, "commit", lambda: commits.append(None))
    monkeypatch.setattr(service.frappe.db, "rollback", lambda: rollbacks.append(None))
    monkeypatch.setattr(recovery, "log_sanitized_error", lambda *args, **kwargs: None)

    result = service.recover_paid_automatic_qr_sales(batch_size=2)

    assert [row["status"] for row in result] == ["submitted", "failed"]
    assert len(commits) == 1
    assert len(rollbacks) == 1
    assert "sale.docstatus = 0" in captured_sql[0]
    assert any("txn.manual_reconciliation_status" in query for query in captured_sql)


def test_persistent_reconciliation_failure_cannot_starve_paid_draft_sales(
    monkeypatch: Any,
) -> None:
    finalized: list[str] = []

    def sql(query: str, *args: Any, **kwargs: Any) -> list[dict[str, str]]:
        if "sale.docstatus = 0" in query and "DESC" in query:
            return [{"name": "MBQR-DRAFT-NEW"}]
        if "sale.docstatus = 0" in query:
            return [{"name": "MBQR-DRAFT-OLD"}]
        if "txn.consumption_key = sale.name" in query:
            return [{"name": "MBQR-RECONCILIATION-STUCK"}]
        return []

    def finalize(name: str) -> dict[str, Any]:
        finalized.append(name)
        if name == "MBQR-RECONCILIATION-STUCK":
            raise RuntimeError("accounting configuration unavailable")
        return {"status": "submitted", "transaction": name}

    monkeypatch.setattr(service.frappe.db, "sql", sql)
    monkeypatch.setattr(recovery, "finalize_paid_automatic_qr_sale", finalize)
    monkeypatch.setattr(service.frappe.db, "commit", lambda: None)
    monkeypatch.setattr(service.frappe.db, "rollback", lambda: None)
    monkeypatch.setattr(recovery, "_recovery_backoff_active", lambda _name: False)
    monkeypatch.setattr(recovery, "_record_recovery_backoff", lambda _name: None)
    monkeypatch.setattr(recovery, "_clear_recovery_backoff", lambda _name: None)
    monkeypatch.setattr(recovery, "log_sanitized_error", lambda *args, **kwargs: None)

    service.recover_paid_automatic_qr_sales(batch_size=4)

    assert finalized[:2] == ["MBQR-DRAFT-NEW", "MBQR-DRAFT-OLD"]
    assert finalized[-1] == "MBQR-RECONCILIATION-STUCK"


def test_recovery_backoff_is_exponential_bounded_and_clearable(
    monkeypatch: Any,
) -> None:
    class FakeCache:
        def __init__(self) -> None:
            self.values: dict[str, Any] = {}
            self.expiries: list[int] = []

        def get_value(self, key: str) -> Any:
            return self.values.get(key)

        def set_value(
            self,
            key: str,
            value: Any,
            *,
            expires_in_sec: int,
        ) -> None:
            self.values[key] = value
            self.expiries.append(expires_in_sec)

        def delete_value(self, key: str) -> None:
            self.values.pop(key, None)

    cache = FakeCache()
    monkeypatch.setattr(service.frappe, "cache", lambda: cache)

    service._record_recovery_backoff("MBQR-STUCK")
    first_delay = cache.expiries[-1]
    service._record_recovery_backoff("MBQR-STUCK")
    second_delay = cache.expiries[-1]

    assert first_delay == service.RECOVERY_BACKOFF_BASE_SECONDS
    assert second_delay == service.RECOVERY_BACKOFF_BASE_SECONDS * 2
    assert service._recovery_backoff_active("MBQR-STUCK") is True
    service._clear_recovery_backoff("MBQR-STUCK")
    assert service._recovery_backoff_active("MBQR-STUCK") is False


def test_accounting_pending_duplicate_incident_uses_recovery_backoff(
    monkeypatch: Any,
) -> None:
    backed_off: list[str] = []
    cleared: list[str] = []
    monkeypatch.setattr(
        recovery,
        "_recovery_candidates",
        lambda _batch_size: [{"name": "MBQR-DUPLICATE"}],
    )
    monkeypatch.setattr(
        recovery,
        "finalize_paid_automatic_qr_sale",
        lambda _name: {
            "status": "payment_incident",
            "settlement_status": "accounting_pending",
        },
    )
    monkeypatch.setattr(service.frappe.db, "commit", lambda: None)
    monkeypatch.setattr(
        recovery,
        "_record_recovery_backoff",
        lambda name: backed_off.append(name),
    )
    monkeypatch.setattr(
        recovery,
        "_clear_recovery_backoff",
        lambda name: cleared.append(name),
    )

    result = service.recover_paid_automatic_qr_sales(batch_size=1)

    assert result[0]["settlement_status"] == "accounting_pending"
    assert backed_off == ["MBQR-DUPLICATE"]
    assert cleared == []


def test_finalization_facade_preserves_stable_hook_and_worker_callables() -> None:
    facade_source = (
        ROOT
        / "kopos_connector/kopos/services/accounting/automatic_qr_finalization_service.py"
    ).read_text(encoding="utf-8")

    assert service.enqueue_automatic_qr_finalization is (
        core.enqueue_automatic_qr_finalization
    )
    assert service.finalize_paid_automatic_qr_sale is (
        core.finalize_paid_automatic_qr_sale
    )
    assert service.recover_paid_automatic_qr_sales is (
        recovery.recover_paid_automatic_qr_sales
    )
    assert "automatic_qr_finalization_core" in facade_source
    assert "automatic_qr_finalization_recovery" in facade_source


def test_scheduler_recovery_callable_keeps_its_stable_import_path() -> None:
    callable_path = (
        "kopos_connector.kopos.services.accounting."
        "automatic_qr_finalization_service.recover_paid_automatic_qr_sales"
    )
    module_name, function_name = callable_path.rsplit(".", 1)
    callable_module = importlib.import_module(module_name)

    assert getattr(callable_module, function_name) is (
        recovery.recover_paid_automatic_qr_sales
    )
