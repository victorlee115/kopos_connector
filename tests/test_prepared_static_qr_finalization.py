from __future__ import annotations

import importlib
import inspect
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fake_frappe import install_fake_frappe_modules


install_fake_frappe_modules()

from kopos_connector.api._maybank_qr_contract import _request_fingerprint
from kopos_connector.api import _maybank_qr_generation as generation
from kopos_connector.api import _maybank_qr_prepared_sale as prepared_sale
from kopos_connector.kopos.services.accounting import (
    prepared_static_qr_finalization as service,
)


class FakeDocument(SimpleNamespace):
    def get(self, fieldname: str) -> Any:
        return getattr(self, fieldname, None)


class FakeOrder(FakeDocument):
    def save(self, *, ignore_permissions: bool) -> None:
        assert ignore_permissions is True
        self.save_count += 1

    def submit(self) -> None:
        self.submit_count += 1
        self.docstatus = 1
        self.status = "Submitted"
        self.invoice_status = "Posted"
        self.stock_status = "Posted"
        self.sales_invoice = "SINV-STATIC-1"
        self.ingredient_stock_entry = "STE-STATIC-1"
        self.payments[0].manual_qr_reconciliation = "MQR-STATIC-1"
        self.payments[0].suspense_account = "QR Suspense - K"


def _payment() -> FakeDocument:
    return FakeDocument(
        name="FBPAY-1",
        source_payment_id="LOCAL-PAYMENT-1",
        payment_method="DuitNow QR",
        payment_channel_code="maybank",
        amount=Decimal("12.50"),
        reference_no=None,
        external_transaction_id=None,
        is_manual_confirmation=0,
        manual_confirmation_evidence_json=None,
        reconciliation_idempotency_key=None,
        settlement_status="awaiting_provider",
        maybank_qr_transaction=None,
        manual_qr_reconciliation=None,
        suspense_account=None,
    )


def _order(*, state: str = "prepared") -> FakeOrder:
    return FakeOrder(
        name="FB-ORDER-1",
        order_id="LOCAL-ORDER-1",
        external_idempotency_key="SALE-IDEMPOTENCY-1",
        accepted_sale_fingerprint="a" * 64,
        automatic_qr_state=state,
        automatic_qr_payment="FBPAY-1",
        automatic_qr_winner_channel=None,
        automatic_qr_static_reconciliation=None,
        device_id="TAB-A001",
        staff_id="cashier@example.test",
        company="KoPOS Sdn Bhd",
        currency="MYR",
        payments=[_payment()],
        docstatus=0,
        status="Draft",
        invoice_status="Pending",
        stock_status="Pending",
        sales_invoice=None,
        ingredient_stock_entry=None,
        save_count=0,
        submit_count=0,
    )


def _request(**overrides: Any) -> dict[str, Any]:
    local_confirmed_at = "2026-07-23T12:00:00+08:00"
    payment_reference = "STATIC-RECEIPT-1"
    values: dict[str, Any] = {
        "confirmation_contract_version": service.CONFIRMATION_CONTRACT_VERSION,
        "money_contract_version": "sen_v1",
        "device_id": "TAB-A001",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FBPAY-1",
        "order_id": "LOCAL-ORDER-1",
        "idempotency_key": "SALE-IDEMPOTENCY-1",
        "accepted_sale_fingerprint": "a" * 64,
        "payment_id": "LOCAL-PAYMENT-1",
        "company": "KoPOS Sdn Bhd",
        "currency": "MYR",
        "amount_sen": 1250,
        "provider_session_id": "static-local-payment-1",
        "payment_reference": payment_reference,
        "local_confirmed_at": local_confirmed_at,
        "manual_confirmation_evidence": {
            "evidence_kind": "no_receipt_acknowledgement",
            "captured_at": local_confirmed_at,
            "upload_status": "not_required",
            "reconciliation_status": "pending_reconciliation",
            "local_confirmed_at": local_confirmed_at,
            "local_confirmed_by": "cashier@example.test",
            "local_confirmation_reference": payment_reference,
            "reconciliation_idempotency_key": "RECONCILE-STATIC-1",
            "evidence_captured_device_id": "TAB-A001",
            "no_receipt_acknowledged": True,
            "no_receipt_reason_code": "customer_declined_receipt",
        },
    }
    values.update(overrides)
    return values


def _attempt(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "MBQR-1",
        "transaction_refno": "MBB-REF-1",
        "status": "pending",
        "maybank_status": 2,
        "qr_data": "issued-maybank-qr",
        "expires_at": "2026-07-23 12:05:00",
        "sale_amount_sen": 1250,
        "currency": "MYR",
        "provider": "maybank_qr",
        "company": "KoPOS Sdn Bhd",
        "device_id": "TAB-A001",
        "outlet_id": "OUTLET-1",
        "idempotency_key": "MAYBANK-ATTEMPT-1",
        "replacement_reason": "",
        "replaces_transaction_refno": "",
        "fb_order": "FB-ORDER-1",
        "fb_order_payment": "FBPAY-1",
        "paid_at": None,
    }
    values.update(overrides)
    values.setdefault(
        "request_fingerprint",
        _request_fingerprint(
            values["device_id"],
            values["idempotency_key"],
            fb_order=values["fb_order"],
            fb_order_payment=values["fb_order_payment"],
            accepted_sale_fingerprint="a" * 64,
            amount_sen=values["sale_amount_sen"],
            currency=values["currency"],
            replacement_reason=values["replacement_reason"],
            replaces_transaction_refno=values["replaces_transaction_refno"],
        ),
    )
    return values


def _install_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    order: FakeOrder,
    attempts: list[dict[str, Any]],
) -> tuple[list[tuple[Any, ...]], list[str]]:
    writes: list[tuple[Any, ...]] = []
    events: list[str] = []
    monkeypatch.setattr(service, "_lock_order", lambda name: events.append("order") or order)
    monkeypatch.setattr(
        service,
        "_load_linked_generation_attempts_for_update",
        lambda order_name, payment_name: events.append("attempts") or attempts,
    )
    monkeypatch.setattr(
        service.frappe.db,
        "set_value",
        lambda *args, **kwargs: writes.append(args),
    )
    return writes, events


def test_static_confirmation_submits_once_and_exact_replay_returns_same_sale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order()
    writes, events = _install_state(monkeypatch, order=order, attempts=[])

    first = service.confirm_prepared_static_qr_payment(_request())
    second = service.confirm_prepared_static_qr_payment(_request())

    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert first["confirmation_contract_version"] == service.CONFIRMATION_CONTRACT_VERSION
    assert first["winner_channel"] == "static_qr"
    assert first["static_claim_role"] == "winning_settlement"
    assert first["static_claim_registered"] is True
    assert first["static_claim_is_sale_winner"] is True
    assert first["winning_maybank_qr_transaction"] is None
    assert first["settlement_state"] == "pending_reconciliation"
    assert first["partial_failure"] is False
    assert first["projection_status"] == "posted"
    assert first["sales_invoice"] == second["sales_invoice"] == "SINV-STATIC-1"
    assert first["ingredient_stock_entry"] == "STE-STATIC-1"
    assert first["maybank_attempts_retained"] is True
    assert order.submit_count == 1
    assert order.payments[0].manual_qr_reconciliation == "MQR-STATIC-1"
    assert events == ["order", "attempts", "order", "attempts"]
    assert writes[0][0:2] == ("FB Order", "FB-ORDER-1")


def test_static_confirmation_after_maybank_winner_registers_one_secondary_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order(state="finalized")
    payment = order.payments[0]
    order.docstatus = 1
    order.status = "Submitted"
    order.invoice_status = "Posted"
    order.stock_status = "Posted"
    order.sales_invoice = "SINV-MAYBANK-1"
    order.ingredient_stock_entry = "STE-MAYBANK-1"
    order.sale_datetime = "2026-07-23 11:55:00"
    order.automatic_qr_winner_channel = "maybank_qr"
    payment.payment_channel_code = "maybank"
    payment.reference_no = "MBB-REF-1"
    payment.external_transaction_id = "MBB-REF-1"
    payment.maybank_qr_transaction = "MBQR-1"
    payment.settlement_status = "verified"
    attempt = _attempt(
        status="paid",
        maybank_status=1,
        paid_at="2026-07-23 11:56:00",
        sales_invoice="SINV-MAYBANK-1",
        consumption_key="FB-ORDER-1",
        invoice_consumption_key="SINV-MAYBANK-1",
        consumed_at="2026-07-23 11:56:01",
    )
    writes, _events = _install_state(
        monkeypatch,
        order=order,
        attempts=[attempt],
    )
    claims: dict[str, FakeDocument] = {}

    monkeypatch.setattr(
        service,
        "_load_secondary_static_claim_for_update",
        lambda key: claims.get(key),
    )

    def get_doc(values: dict[str, Any]) -> FakeDocument:
        claim = FakeDocument(**values, name="MQR-SECONDARY-1")

        def insert(*, ignore_permissions: bool) -> None:
            assert ignore_permissions is True
            claims[claim.reconciliation_idempotency_key] = claim

        claim.insert = insert
        return claim

    monkeypatch.setattr(service.frappe, "get_doc", get_doc)

    first = service.confirm_prepared_static_qr_payment(_request())
    second = service.confirm_prepared_static_qr_payment(_request())

    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert first["winner_channel"] == "maybank_qr"
    assert first["settlement_state"] == "pending_reconciliation"
    assert first["static_claim_role"] == "secondary_possible_duplicate"
    assert first["static_claim_status"] == "pending_reconciliation"
    assert first["static_claim_registered"] is True
    assert first["static_claim_is_sale_winner"] is False
    assert first["winning_maybank_qr_transaction"] == "MBQR-1"
    assert first["winning_payment_settlement_state"] == "verified"
    assert first["manual_qr_reconciliation"] == "MQR-SECONDARY-1"
    assert first["sales_invoice"] == second["sales_invoice"] == "SINV-MAYBANK-1"
    assert first["ingredient_stock_entry"] == "STE-MAYBANK-1"
    assert len(claims) == 1
    claim = claims["RECONCILE-STATIC-1"]
    assert claim.claim_role == "secondary_possible_duplicate"
    assert claim.finance_resolution_status == "pending_review"
    assert claim.winning_maybank_qr_transaction == "MBQR-1"
    assert claim.suspense_account is None
    assert getattr(claim, "reclassification_journal_entry", None) is None
    assert getattr(claim, "failure_journal_entry", None) is None
    assert order.submit_count == 0
    assert order.save_count == 0
    assert payment.payment_channel_code == "maybank"
    assert payment.maybank_qr_transaction == "MBQR-1"
    assert payment.settlement_status == "verified"
    assert writes == []


def test_secondary_static_claim_replay_rejects_changed_stored_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order(state="finalized")
    payment = order.payments[0]
    order.docstatus = 1
    order.status = "Submitted"
    order.invoice_status = "Posted"
    order.stock_status = "Posted"
    order.sales_invoice = "SINV-MAYBANK-1"
    order.sale_datetime = "2026-07-23 11:55:00"
    order.automatic_qr_winner_channel = "maybank_qr"
    payment.payment_channel_code = "maybank"
    payment.reference_no = "MBB-REF-1"
    payment.external_transaction_id = "MBB-REF-1"
    payment.maybank_qr_transaction = "MBQR-1"
    payment.settlement_status = "verified"
    attempt = _attempt(
        status="paid",
        maybank_status=1,
        paid_at="2026-07-23 11:56:00",
        sales_invoice="SINV-MAYBANK-1",
        consumption_key="FB-ORDER-1",
        invoice_consumption_key="SINV-MAYBANK-1",
        consumed_at="2026-07-23 11:56:01",
    )
    _install_state(monkeypatch, order=order, attempts=[attempt])
    claim = FakeDocument(
        name="MQR-SECONDARY-1",
        status="pending_reconciliation",
        claim_role="secondary_possible_duplicate",
        winning_maybank_qr_transaction="MBQR-1",
        fb_order=order.name,
        sales_invoice=order.sales_invoice,
        fb_order_payment=payment.name,
        device_id=order.device_id,
        staff_id=order.staff_id,
        company=order.company,
        currency=order.currency,
        business_date="2026-07-23",
        amount_sen=1250,
        payment_reference="CHANGED-REFERENCE",
        provider_session_id="static-local-payment-1",
        reconciliation_idempotency_key="RECONCILE-STATIC-1",
        suspense_account=None,
        evidence_kind="no_receipt_acknowledgement",
        evidence_captured_at="2026-07-23 12:00:00",
        evidence_json=service._parse_request(_request())["normalized_payment"][
            "manual_confirmation_evidence_json"
        ],
        reclassification_journal_entry=None,
        failure_journal_entry=None,
    )
    monkeypatch.setattr(
        service,
        "_load_secondary_static_claim_for_update",
        lambda _key: claim,
    )

    with pytest.raises(service.frappe.ValidationError):
        service.confirm_prepared_static_qr_payment(_request())

    assert order.submit_count == 0


@pytest.mark.parametrize(
    ("state", "attempt_overrides"),
    [
        ("provider_pending", {}),
        ("provider_pending", {"qr_data": ""}),
        ("provider_ambiguous", {"status": "unknown", "maybank_status": 0}),
        ("provider_rejected", {"status": "failed", "maybank_status": 0, "qr_data": ""}),
    ],
)
def test_issued_no_display_ambiguous_and_rejected_attempts_stay_linked(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    attempt_overrides: dict[str, Any],
) -> None:
    order = _order(state=state)
    attempt = _attempt(**attempt_overrides)
    _install_state(monkeypatch, order=order, attempts=[attempt])

    result = service.confirm_prepared_static_qr_payment(_request())

    assert result["status"] == "ok"
    assert result["maybank_attempt_count"] == 1
    assert result["maybank_attempts_retained"] is True
    assert attempt["fb_order"] == "FB-ORDER-1"
    assert attempt["fb_order_payment"] == "FBPAY-1"


@pytest.mark.parametrize(
    "attempt_overrides",
    [
        {"device_id": "OTHER-TABLET"},
        {"company": "Other Company"},
        {"currency": "SGD"},
        {"sale_amount_sen": 1251},
        {"fb_order": "FB-ORDER-OTHER"},
        {"fb_order_payment": "FBPAY-OTHER"},
        {"request_fingerprint": "f" * 64},
    ],
)
def test_mismatched_provider_attempt_identity_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
    attempt_overrides: dict[str, Any],
) -> None:
    order = _order()
    attempt = _attempt(**attempt_overrides)
    _install_state(monkeypatch, order=order, attempts=[attempt])

    with pytest.raises(service.frappe.ValidationError):
        service.confirm_prepared_static_qr_payment(_request())

    assert order.submit_count == 0


def test_paid_attempt_is_registered_only_after_static_sale_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order(state="provider_paid")
    attempt = _attempt(
        status="paid",
        maybank_status=1,
        paid_at="2026-07-23 12:01:00",
    )
    _install_state(monkeypatch, order=order, attempts=[attempt])
    observed: list[tuple[int, str, list[str]]] = []

    def register(
        paid_attempts: list[Any],
        *,
        order_doc: Any,
        winning_transaction_name: str,
    ) -> list[str]:
        observed.append(
            (
                order_doc.docstatus,
                order_doc.automatic_qr_static_reconciliation,
                [row["name"] for row in paid_attempts],
            )
        )
        assert winning_transaction_name == ""
        return []

    monkeypatch.setattr(
        service,
        "_register_late_paid_incidents_after_sale_commit",
        register,
    )

    result = service.confirm_prepared_static_qr_payment(_request())

    assert result["status"] == "ok"
    assert observed == [(1, "MQR-STATIC-1", ["MBQR-1"])]
    assert order.submit_count == 1


def test_exact_request_identity_and_evidence_are_mandatory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order()
    _install_state(monkeypatch, order=order, attempts=[])

    for changed in (
        {"device_id": "OTHER-TABLET"},
        {"amount_sen": 1251},
        {"company": "Other Company"},
        {"accepted_sale_fingerprint": "b" * 64},
        {"payment_id": "OTHER-PAYMENT"},
        {"local_confirmed_at": "2026-07-23T12:00:01+08:00"},
    ):
        with pytest.raises(service.frappe.ValidationError):
            service.confirm_prepared_static_qr_payment(_request(**changed))

    assert order.submit_count == 0


def test_delayed_ambiguous_provider_result_cannot_regress_static_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserved = {
        "name": "MBQR-1",
        "status": "creating",
        "transaction_refno": "creating-placeholder",
        "fb_order": "FB-ORDER-1",
    }
    writes: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        generation,
        "_load_reserved_txn_with_order_lock",
        lambda fingerprint: reserved,
    )

    def get_value(doctype: str, name: str, fields: Any, **kwargs: Any) -> Any:
        assert kwargs.get("as_dict") is True
        return {
            "name": name,
            "docstatus": 1,
            "automatic_qr_winner_channel": "static_qr",
        }

    monkeypatch.setattr(generation.frappe.db, "get_value", get_value)
    monkeypatch.setattr(
        generation.frappe.db,
        "set_value",
        lambda *args, **kwargs: writes.append(args),
    )

    generation._mark_generation_ambiguous("f" * 64, "provider timeout")

    assert writes[0][0:2] == ("Maybank QR Transaction", "MBQR-1")
    assert not any(write[0] == "FB Order" for write in writes)


def test_generation_starting_after_static_winner_returns_typed_preflight_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _order()
    payment = order.payments[0]
    order.docstatus = 1
    order.status = "Submitted"
    order.automatic_qr_state = "finalized"
    order.automatic_qr_winner_channel = "static_qr"
    order.automatic_qr_static_reconciliation = "MQR-STATIC-1"
    payment.payment_channel_code = "static_qr"
    payment.is_manual_confirmation = 1
    payment.manual_qr_reconciliation = "MQR-STATIC-1"
    payment.maybank_qr_transaction = None
    payment.reference_no = "STATIC-RECEIPT-1"
    payment.external_transaction_id = "static-local-payment-1"

    monkeypatch.setattr(
        prepared_sale.frappe.db,
        "sql",
        lambda *_args, **_kwargs: [(order.name,)],
    )
    monkeypatch.setattr(
        prepared_sale.frappe,
        "get_doc",
        lambda doctype, name: order,
    )
    monkeypatch.setattr(
        prepared_sale,
        "_load_linked_generation_attempts_for_update",
        lambda *_args: [],
    )

    result = prepared_sale.load_prepared_automatic_qr_sale(
        fb_order=order.name,
        fb_order_payment=payment.name,
        accepted_sale_fingerprint=order.accepted_sale_fingerprint,
        device_id=order.device_id,
        amount_sen=1250,
        idempotency_key="MAYBANK-LATE-BACKGROUND-1",
        currency="MYR",
        replacement_request=None,
        validate_generation_attempt=generation._validate_new_generation_attempt,
    )

    rejection = result["preflight_rejection"]
    assert isinstance(rejection, generation.MaybankQrPreflightRejection)
    assert rejection.reason_code == "prepared_sale_static_winner"
    assert result["fb_order"] == order.name
    assert result["fb_order_payment"] == payment.name


def test_public_endpoint_is_post_only_and_locks_device_before_sale() -> None:
    api = importlib.import_module("kopos_connector.api")
    auth = importlib.import_module("kopos_connector.auth")
    path = (
        "/api/method/kopos_connector.api."
        "confirm_prepared_automatic_qr_static_payment"
    )

    assert path in auth.ALLOWED_DEVICE_API_PATHS
    assert auth.DEVICE_API_HTTP_METHODS[path] == frozenset({"POST"})
    source = inspect.getsource(
        api.confirm_prepared_automatic_qr_static_payment
    )
    assert source.index("lock_device_for_operational_mutation") < source.index(
        "confirm_prepared_static_qr_payment(payload)"
    )
